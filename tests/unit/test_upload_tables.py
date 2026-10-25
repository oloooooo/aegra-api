"""`POST /custom/uploads/tables` 的路由逻辑（不连库、不连 S3）。

这里锁的是「写序与失败清理」：提交失败时只能删**本次新写**的对象，且它不能是
旧行正在用的那一个——否则同名同内容重传恰好卡在配额，就把旧行唯一那份字节删了。
"""

import io

import pytest
from fastapi import HTTPException

from aegra_api.models.auth import User
from custom import blobs, session_objects
from custom import uploads as uploads_mod


class FakeFile:
    def __init__(self, name: str, data: bytes = b"x"):
        self.filename = name
        self.size = len(data)
        self.file = io.BytesIO(data)


class FakeSession:
    def __init__(self, old_key: str | None = None):
        self.committed = False
        self.old_key = old_key

    async def scalar(self, *_a, **_kw):
        return "t-1"  # 会话归属校验：存在

    async def commit(self):
        self.committed = True


@pytest.fixture
def env(monkeypatch):
    """把 blobs 与 session_objects 换成记录调用的替身。"""
    calls = {"put": [], "copy": [], "delete": []}
    monkeypatch.setattr(blobs, "put_stream", lambda src, key, *, max_bytes: calls["put"].append(key) or ("a" * 64, 10))
    monkeypatch.setattr(blobs, "copy", lambda src, dst: calls["copy"].append((src, dst)))
    monkeypatch.setattr(blobs, "delete", lambda key: calls["delete"].append(key))

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


async def test_happy_path_writes_then_cleans_tmp(env):
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
    # 写序：先临时 key，再 copy 到正式 key，最后删临时 key
    assert calls["put"][0].startswith("tmp/")
    assert calls["copy"][0][0] == calls["put"][0]
    assert calls["copy"][0][1].startswith("sessions/")
    assert calls["delete"] == [calls["put"][0]]


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


async def test_quota_failure_deletes_new_object(env):
    calls, state = env
    state["raise"] = session_objects.QuotaExceeded("满了")
    with pytest.raises(HTTPException) as e:
        await uploads_mod.upload_session_table(
            session=FakeSession(), user=_user("sp"), thread_id="t-1", file=FakeFile("sp_表.xlsx")
        )
    assert e.value.status_code == 413
    assert calls["copy"][0][1] in calls["delete"]  # 新对象被清掉


async def test_quota_failure_keeps_object_when_it_is_the_old_one(env):
    """同内容重传 + 配额失败：新 key 就是旧行在用的那个，不能删。"""
    calls, state = env
    state["old_key"] = blobs.blob_key("u1", "t-1", "a" * 64, "sp_表.xlsx")
    state["raise"] = session_objects.QuotaExceeded("满了")

    with pytest.raises(HTTPException):
        await uploads_mod.upload_session_table(
            session=FakeSession(), user=_user("sp"), thread_id="t-1", file=FakeFile("sp_表.xlsx")
        )

    assert calls["copy"][0][1] == state["old_key"]  # 前提：新旧 key 确实相同
    assert calls["copy"][0][1] not in calls["delete"]


async def test_cleanup_failure_does_not_mask_the_real_error(env, monkeypatch):
    """存储挂了时清理也会挂：必须仍然返回 503，而不是把异常顶出去变成 500。"""
    calls, _ = env
    monkeypatch.setattr(session_objects, "MAX_BLOB_BYTES", 10 * 1024 * 1024)

    def put_boom(*_a, **_kw):
        raise blobs.BlobUnavailable("S3_ENDPOINT 未配置")

    monkeypatch.setattr(blobs, "put_stream", put_boom)

    def delete_boom(_key):
        raise blobs.BlobUnavailable("S3_ENDPOINT 未配置")

    monkeypatch.setattr(blobs, "delete", delete_boom)

    with pytest.raises(HTTPException) as e:
        await uploads_mod.upload_session_table(
            session=FakeSession(), user=_user("sp"), thread_id="t-1", file=FakeFile("sp_表.xlsx")
        )
    assert e.value.status_code == 503
