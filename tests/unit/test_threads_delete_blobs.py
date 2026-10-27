"""删会话时的二进制清理：先读台账拿 key、删行，再逐对象删；任一步失败都不报错。"""

import pytest

from custom.middleware import threads_delete
from custom.service import session_files as session_objects
from shared.session import blobs


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
    calls: dict[str, list] = {"rows": [], "keys": [], "deleted": []}
    session = FakeSession()

    monkeypatch.setattr(threads_delete, "get_session_maker", lambda: lambda: FakeCM(session))

    async def fake_list_rows(_s, owner, thread_id):
        calls["rows"].append((owner, thread_id))
        return [FakeRow("uploads/u1/a.xlsx"), FakeRow("uploads/u2/b.xlsx")]

    async def fake_delete_rows(_s, owner, thread_id):
        calls["keys"].append((owner, thread_id))
        return 2

    monkeypatch.setattr(session_objects, "list_for_thread", fake_list_rows)
    monkeypatch.setattr(session_objects, "delete_for_thread", fake_delete_rows)
    monkeypatch.setattr(blobs, "configured", lambda: True)  # 默认当“生产”、单测自带一个
    monkeypatch.setattr(blobs, "delete_many", lambda keys: calls["deleted"].append(list(keys)) or len(keys))
    return calls, session


async def test_clears_rows_then_objects(wired):
    calls, session = wired

    await threads_delete._clear_blobs("u1", "t-1")

    assert calls["rows"] == [("u1", "t-1")]
    assert calls["keys"] == [("u1", "t-1")]
    assert calls["deleted"] == [["uploads/u1/a.xlsx", "uploads/u2/b.xlsx"]]
    assert session.committed is True


async def test_row_failure_skips_objects_and_swallows(wired, monkeypatch):
    calls, _ = wired

    async def boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(session_objects, "delete_for_thread", boom)

    await threads_delete._clear_blobs("u1", "t-1")  # 不抛

    assert calls["deleted"] == []  # 行没删掉就不要动对象，否则「行说 A、字节没了」


async def test_object_failure_is_swallowed(wired, monkeypatch):
    calls, _ = wired

    def boom(_keys):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(blobs, "delete_many", boom)

    await threads_delete._clear_blobs("u1", "t-1")  # 不抛

    assert calls["keys"] == [("u1", "t-1")]
