"""`/uploads/` 只读：写家族必须同步与异步两套都被拦住（ADR-0018 缺陷 13 的回归测试）。

为什么两套都要测：`StoreBackend` 自己同时实现了 `write` 与 `awrite`，只覆写同步版对工具
（走 async）无效——那样写会静默落进 LangGraph Store，而 xlsx 工具读的是盘上台账指向的字节。
"""

from __future__ import annotations

import pytest
from langgraph.store.memory import InMemoryStore

from graphs.access import session_scope, uploads_backend
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


class _Row:
    """台账行的最小形状（真行是 ORM 对象，这里只要 `ls`/`read` 用到的两个字段）。"""

    def __init__(self, object_key: str, size_bytes: int) -> None:
        self.object_key = object_key
        self.size_bytes = size_bytes


def _ledger(monkeypatch: pytest.MonkeyPatch, rows: dict[str, _Row]) -> None:
    async def fake() -> dict[str, _Row]:
        return rows

    monkeypatch.setattr(uploads_backend, "_ledger", fake)


async def test_als_lists_tables_from_ledger(backend: UploadsBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    """上传的表也是 `/uploads/` 里的文件：`ls` 必须列出来。

    否则没有清单的子 agent 永远发现不了上传的表（ADR-0018 决定 8 / 缺陷 5）。
    """
    _ledger(monkeypatch, {"/t.xlsx": _Row("a" * 32 + "/t.xlsx", 7)})
    ls = await backend.als("/")
    assert [(e["path"], e.get("size")) for e in (ls.entries or [])] == [("/t.xlsx", 7)]


async def test_aread_on_table_points_at_xlsx_tools(
    backend: UploadsBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`read_file` 对表不能回「没有这个文件」——它就在那儿，只是要用 xlsx 工具。"""
    _ledger(monkeypatch, {"/t.xlsx": _Row("b" * 32 + "/t.xlsx", 7)})
    result = await backend.aread("/t.xlsx")
    assert result.error and "xlsx_info" in result.error
    # 台账里没有的路径仍然交回 Store（md 的 file_not_found 语义不变）
    assert (await backend.aread("/nope.md")).error
