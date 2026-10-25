"""删会话时的二进制清理（ADR-0015 §6）：先删行，再删对象前缀；任一步失败都不报错。"""

import pytest

from custom import blobs, session_objects, threads_delete


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


@pytest.fixture
def wired(monkeypatch):
    calls: dict[str, list] = {"rows": [], "prefix": []}
    session = FakeSession()

    class FakeDB:
        @staticmethod
        def get_session_maker():
            return lambda: FakeCM(session)

    monkeypatch.setattr(threads_delete, "db_manager", FakeDB)

    async def fake_delete_rows(_s, owner, thread_id):
        calls["rows"].append((owner, thread_id))
        return 2

    monkeypatch.setattr(session_objects, "delete_for_thread", fake_delete_rows)
    monkeypatch.setattr(blobs, "delete_prefix", lambda prefix: calls["prefix"].append(prefix) or 3)
    return calls, session


async def test_clears_rows_then_prefix(wired):
    calls, session = wired

    await threads_delete._clear_blobs("u1", "t-1")

    assert calls["rows"] == [("u1", "t-1")]
    assert calls["prefix"] == [blobs.session_prefix("u1", "t-1")]
    assert session.committed is True


async def test_row_failure_skips_prefix_and_swallows(wired, monkeypatch):
    calls, _ = wired

    async def boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(session_objects, "delete_for_thread", boom)

    await threads_delete._clear_blobs("u1", "t-1")  # 不抛

    assert calls["prefix"] == []  # 行没删掉就不要动对象，否则「行说 A、字节没了」


async def test_prefix_failure_is_swallowed(wired, monkeypatch):
    calls, _ = wired

    def boom(_prefix):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(blobs, "delete_prefix", boom)

    await threads_delete._clear_blobs("u1", "t-1")  # 不抛

    assert calls["rows"] == [("u1", "t-1")]
