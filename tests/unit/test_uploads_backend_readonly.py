"""`/uploads/` 只读：写家族必须同步与异步两套都被拦住（ADR-0018 缺陷 13 的回归测试）。

为什么两套都要测：`StoreBackend` 自己同时实现了 `write` 与 `awrite`，只覆写同步版对工具
（走 async）无效——那样写会静默落进 LangGraph Store，而 xlsx 工具读的是盘上台账指向的字节。
"""

from __future__ import annotations

import pytest
from langgraph.store.memory import InMemoryStore

from graphs.access import session_scope
from graphs.access.uploads_backend import UploadsBackend


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> UploadsBackend:
    # 不碰真 PG / 真图：Store 用内存实现，命名空间函数原样用（写家族不看它）
    monkeypatch.setattr(session_scope, "uploads_namespace", lambda _rt: ("uploads", "u1", "t1"))
    b = UploadsBackend()
    b._store = InMemoryStore()
    return b


def test_sync_write_family_is_refused(backend: UploadsBackend) -> None:
    assert backend.write("/a.md", "x").error
    assert backend.edit("/a.md", "x", "y").error
    assert backend.delete("/a.md").error
    assert backend.upload_files([("/a.md", b"x")])[0].error


async def test_async_write_family_is_refused(backend: UploadsBackend) -> None:
    assert (await backend.awrite("/x.xlsx", "text")).error
    assert (await backend.aedit("/x.xlsx", "a", "b")).error
    assert (await backend.adelete("/x.xlsx")).error
    assert (await backend.aupload_files([("/x.xlsx", b"x")]))[0].error


async def test_nothing_lands_in_store(backend: UploadsBackend) -> None:
    """拒绝必须是「没写进去」，不只是「返回了 error」。"""
    await backend.awrite("/x.xlsx", "text")
    ls = await backend.als("/")
    assert [e["path"] for e in (ls.entries or [])] == []
