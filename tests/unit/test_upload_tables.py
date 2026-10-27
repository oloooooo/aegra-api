"""`POST /custom/uploads/tables` 的路由逻辑（不连库、不碰盘）。

这里锁的是「写序与失败清理」：提交失败时只能删**本次新写**的对象。
key 每次都是新的 `<uuid>/<名>`，所以永远不会删掉旧行在用的那份字节。
"""

import io

import pytest
from fastapi import HTTPException

from aegra_api.models.auth import User
from custom.api import uploads as uploads_mod
from custom.service import session_files as session_objects
from shared.session import disk

NEW_KEY = "0123456789abcdef0123456789abcdef/sp_表.xlsx"
OLD_KEY = "fedcba9876543210fedcba9876543210/sp_表.xlsx"


class FakeFile:
    def __init__(self, name: str, data: bytes = b"x"):
        self.filename = name
        self.size = len(data)
        self.file = io.BytesIO(data)


class FakeSession:
    def __init__(self):
        self.committed = False

    async def scalar(self, *_a, **_kw):
        return "t-1"  # 会话归属校验：存在

    async def commit(self):
        self.committed = True


@pytest.fixture
def env(monkeypatch):
    """把 disk 与 session_objects 换成记录调用的替身。"""
    calls = {"put": [], "delete": []}
    monkeypatch.setattr(disk, "new_key", lambda name: NEW_KEY)
    monkeypatch.setattr(
        disk, "put_stream", lambda src, key, *, max_bytes: calls["put"].append(key) or ("a" * 64, 10)
    )
    monkeypatch.setattr(disk, "delete", lambda key: calls["delete"].append(key) or True)

    state = {"old_key": None, "raise": None}

    async def fake_get_one(_s, _o, _t, _n):
        return type("Row", (), {"object_key": state["old_key"]})() if state["old_key"] else None

    async def fake_upsert(_s, **kw):
        if state["raise"]:
            raise state["raise"]
        return state["old_key"]

    monkeypatch.setattr(session_objects, "get_one", fake_get_one)
    monkeypatch.setattr(session_objects, "upsert", fake_upsert)
    return calls, state


def _user(*spaces):
    return User(identity="u1", user_spaces=list(spaces))


async def test_happy_path_writes_one_new_key(env):
    calls, _ = env
    session = FakeSession()

    resp = await uploads_mod.upload_session_table(
        session=session, user=_user("sp"), thread_id="t-1", file=FakeFile("sp_表.xlsx")
    )

    assert resp.name == "sp_表.xlsx"
    assert resp.path == "/uploads/sp_表.xlsx"
    assert resp.space == "sp"
    assert resp.bytes == 10
    assert resp.indexed is False
    assert session.committed is True
    # 只写一个新 key，没有临时 key / 复制两步（`.part` → rename 在 disk 里）
    assert calls["put"] == [NEW_KEY]
    assert calls["delete"] == []  # 首次上传没有旧对象可删


async def test_overwrite_deletes_replaced_object(env):
    calls, state = env
    state["old_key"] = OLD_KEY

    await uploads_mod.upload_session_table(
        session=FakeSession(), user=_user("sp"), thread_id="t-1", file=FakeFile("sp_表.xlsx")
    )

    assert calls["put"] == [NEW_KEY]
    assert calls["delete"] == [OLD_KEY]  # 提交成功后才删被替换的旧对象


async def test_rejects_bad_prefix(env):
    with pytest.raises(HTTPException) as e:
        await uploads_mod.upload_session_table(
            session=FakeSession(), user=_user("sp"), thread_id="t-1", file=FakeFile("没有前缀.xlsx")
        )
    assert e.value.status_code == 422


async def test_rejects_space_not_owned(env):
    with pytest.raises(HTTPException) as e:
        await uploads_mod.upload_session_table(
            session=FakeSession(), user=_user("other"), thread_id="t-1", file=FakeFile("sp_表.xlsx")
        )
    assert e.value.status_code == 422
    assert "不是你的空间" in e.value.detail


async def test_rejects_non_xlsx(env):
    with pytest.raises(HTTPException) as e:
        await uploads_mod.upload_session_table(
            session=FakeSession(), user=_user("sp"), thread_id="t-1", file=FakeFile("sp_表.md")
        )
    assert e.value.status_code == 415


async def test_too_large_is_rejected_before_reading(env, monkeypatch):
    monkeypatch.setattr(session_objects, "MAX_BLOB_BYTES", 5)
    monkeypatch.setattr(session_objects, "MAX_BLOB_MB", 0)
    calls, _ = env
    with pytest.raises(HTTPException) as e:
        await uploads_mod.upload_session_table(
            session=FakeSession(), user=_user("sp"), thread_id="t-1", file=FakeFile("sp_表.xlsx", b"123456")
        )
    assert e.value.status_code == 413
    assert calls["put"] == []  # 一个字节都没读


async def test_stream_too_large_deletes_new_object(env, monkeypatch):
    """file.size 缺失或撒谎时由 put_stream 兜：错误是 413，新写的残片被清掉。"""
    calls, _ = env
    monkeypatch.setattr(session_objects, "MAX_BLOB_BYTES", 10 * 1024 * 1024)

    def boom(_src, key, *, max_bytes):
        calls["put"].append(key)
        raise disk.TooLarge("文件超过 0MB 上限")

    monkeypatch.setattr(disk, "put_stream", boom)

    with pytest.raises(HTTPException) as e:
        await uploads_mod.upload_session_table(
            session=FakeSession(), user=_user("sp"), thread_id="t-1", file=FakeFile("sp_表.xlsx")
        )
    assert e.value.status_code == 413
    assert calls["delete"] == [NEW_KEY]


async def test_quota_failure_deletes_new_object_only(env):
    calls, state = env
    state["old_key"] = OLD_KEY
    state["raise"] = session_objects.QuotaExceeded("满了")

    with pytest.raises(HTTPException) as e:
        await uploads_mod.upload_session_table(
            session=FakeSession(), user=_user("sp"), thread_id="t-1", file=FakeFile("sp_表.xlsx")
        )

    assert e.value.status_code == 413
    assert calls["delete"] == [NEW_KEY]  # 只删新对象，旧行那份字节不动


async def test_cleanup_failure_does_not_mask_the_real_error(env, monkeypatch):
    """清理自己也会失败：必须仍然把原始错误顶出去，而不是被清理异常盖掉。"""
    calls, _ = env
    monkeypatch.setattr(session_objects, "MAX_BLOB_BYTES", 10 * 1024 * 1024)

    def put_boom(_src, key, *, max_bytes):
        calls["put"].append(key)
        raise OSError("磁盘满了")

    def delete_boom(_key):
        raise OSError("磁盘满了")

    monkeypatch.setattr(disk, "put_stream", put_boom)
    monkeypatch.setattr(disk, "delete", delete_boom)

    with pytest.raises(OSError, match="磁盘满了"):
        await uploads_mod.upload_session_table(
            session=FakeSession(), user=_user("sp"), thread_id="t-1", file=FakeFile("sp_表.xlsx")
        )
