"""删会话时的二进制清理（ADR-0018 §6）：先读出 key、删行，再逐对象删；任一步失败都不报错。"""

import pytest

from custom.middleware import threads_delete
from custom.service import session_files as session_objects
from shared.session import disk


class FakeSession:
    def __init__(self):
        self.committed = False

    async def commit(self):
        self.committed = True


class FakeCM:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


class FakeRow:
    def __init__(self, key):
        self.object_key = key


@pytest.fixture
def wired(monkeypatch):
    calls: dict[str, list] = {"rows": [], "deleted": []}
    session = FakeSession()

    monkeypatch.setattr(threads_delete, "get_session_maker", lambda: lambda: FakeCM(session))

    async def fake_list(_s, owner, thread_id):
        calls["rows"].append((owner, thread_id))
        return [FakeRow("aa/x.xlsx"), FakeRow("bb/y.xlsx")]

    async def fake_delete_rows(_s, owner, thread_id):
        return 2

    monkeypatch.setattr(session_objects, "list_for_thread", fake_list)
    monkeypatch.setattr(session_objects, "delete_for_thread", fake_delete_rows)
    monkeypatch.setattr(disk, "delete_many", lambda keys: calls["deleted"].extend(keys) or len(keys))
    return calls, session


async def test_reads_keys_then_deletes_objects(wired):
    """key 是 `<uuid>/<名>`，不带会话前缀：只能先读台账再逐个删。"""
    calls, session = wired

    await threads_delete._clear_files("u1", "t-1")

    assert calls["rows"] == [("u1", "t-1")]
    assert calls["deleted"] == ["aa/x.xlsx", "bb/y.xlsx"]
    assert session.committed is True


async def test_row_failure_skips_objects_and_swallows(wired, monkeypatch):
    calls, _ = wired

    async def boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(session_objects, "delete_for_thread", boom)

    await threads_delete._clear_files("u1", "t-1")  # 不抛

    assert calls["deleted"] == []  # 行没删掉就不要动对象，否则「行说 A、字节没了」


async def test_object_failure_is_swallowed(wired, monkeypatch):
    calls, _ = wired

    def boom(_keys):
        raise RuntimeError("disk down")

    monkeypatch.setattr(disk, "delete_many", boom)

    await threads_delete._clear_files("u1", "t-1")  # 不抛

    assert calls["rows"] == [("u1", "t-1")]


async def test_no_rows_means_no_object_deletes(wired, monkeypatch):
    """没有二进制行（只传过 md）时不该去碰盘。"""
    calls, _ = wired

    async def empty(_s, _owner, _thread_id):
        return []

    monkeypatch.setattr(session_objects, "list_for_thread", empty)

    await threads_delete._clear_files("u1", "t-1")

    assert calls["deleted"] == []
