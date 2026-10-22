"""发件箱 ack 接口：把行从 pending 原子翻成 delivered（ADR-0010）。

回归的旧 bug：认领表用「同线程 8 秒内已有别的任务认领」当让路窗口，而查询只看
parent_thread_id、认不出调用方，于是同一 hook 里第 1 个任务一认领就把同批后面的
全卡住——三个子 agent 几乎同时完成只投出一个。
新语义是单行条件 UPDATE：一行一判，同批任务互不影响，没有时间窗口。
"""

from __future__ import annotations

from typing import Any

import pytest

from aegra_api.models.auth import User
from custom.delivery_api import claim_delivery, release_delivery

PARENT = "parent-thread"


class _Rows:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class FakeSession:
    """够 delivery_api 用：区分「线程属主查询」与「行状态查询」。"""

    def __init__(self, *, row_status: str | None, flip_ok: bool, insert_ok: bool = False) -> None:
        self.row_status = row_status
        self.flip_ok = flip_ok
        self.insert_ok = insert_ok
        self.updates: list[str] = []

    async def scalar(self, stmt: Any) -> Any:
        sql = str(stmt)
        if "FROM thread" in sql:
            return "root"  # 线程属主
        if "FROM async_task_outbox" in sql:
            return self.row_status
        return None

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        sql = str(stmt)
        self.updates.append(sql)
        if sql.startswith("UPDATE"):
            # 第一次 UPDATE 是翻牌；release 的 UPDATE 不关心返回值
            return _Rows("task-a" if self.flip_ok else None)
        if sql.startswith("INSERT"):
            return _Rows("task-a" if self.insert_ok else None)
        return _Rows(None)

    async def commit(self) -> None:
        return None


def _user() -> User:
    return User(identity="root")


@pytest.mark.asyncio
async def test_pending_row_is_claimed() -> None:
    session = FakeSession(row_status="pending", flip_ok=True)
    out = await claim_delivery("task-a", {"parent_thread_id": PARENT, "run_id": "run-a"}, session, _user())  # type: ignore[arg-type]
    assert out == {"claimed": True, "reason": "claimed"}


@pytest.mark.asyncio
async def test_already_delivered_row_is_skipped() -> None:
    """别人已经投过 → 放弃。没有时间窗口，看的是状态。"""
    session = FakeSession(row_status="delivered", flip_ok=False)
    out = await claim_delivery("task-a", {"parent_thread_id": PARENT, "run_id": "run-a"}, session, _user())  # type: ignore[arg-type]
    assert out == {"claimed": False, "reason": "delivered"}


@pytest.mark.asyncio
async def test_failed_row_can_still_be_claimed() -> None:
    """分发者重试耗尽只说明没人叫醒父线程；结果内容还在，谁写进 state 都比丢掉强。"""
    session = FakeSession(row_status="failed", flip_ok=True)
    out = await claim_delivery("task-a", {"parent_thread_id": PARENT, "run_id": "run-a"}, session, _user())  # type: ignore[arg-type]
    assert out == {"claimed": True, "reason": "claimed"}


@pytest.mark.asyncio
async def test_missing_row_is_created_for_backward_compat() -> None:
    """发件箱上线之前产生的终态、或不经过 finalize_run 的边缘路径仍然要能投。"""
    session = FakeSession(row_status=None, flip_ok=False, insert_ok=True)
    out = await claim_delivery("task-a", {"parent_thread_id": PARENT, "run_id": "run-a"}, session, _user())  # type: ignore[arg-type]
    assert out == {"claimed": True, "reason": "inserted"}


@pytest.mark.asyncio
async def test_batch_of_rows_does_not_self_block() -> None:
    """同一批多个任务各自翻各自的牌——这正是旧版 8s 窗口挡住的地方。"""
    for task in ("task-a", "task-b", "task-c"):
        session = FakeSession(row_status="pending", flip_ok=True)
        out = await claim_delivery(task, {"parent_thread_id": PARENT, "run_id": f"run-{task}"}, session, _user())  # type: ignore[arg-type]
        assert out["claimed"] is True


@pytest.mark.asyncio
async def test_release_puts_row_back_to_pending() -> None:
    session = FakeSession(row_status="delivered", flip_ok=False)
    out = await release_delivery("task-a", "run-a", PARENT, session, _user())  # type: ignore[arg-type]
    assert out == {"released": True}
    assert any("UPDATE" in sql for sql in session.updates)
