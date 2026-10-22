"""发件箱分发者：谁能开唤醒 run（ADR-0010）。

不变量：**只有分发者会为子任务结果开唤醒 run**，而它靠三件事保证不重复、不丢：
1. 父线程有活跃 run 时不动手（那轮的 hook 会写 state 并 ack）；
2. 「查活跃 run → 建 run」在同一事务里锁住 thread 行，跨进程不会都通过检查；
3. 唤醒 run 用确定性 run_id，重试撞主键 = 上次已经建过，按已投递处理。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from custom import outbox_dispatcher as disp

PARENT = "parent-thread"


class FakeRun:
    def __init__(self, task_id: str, run_id: str) -> None:
        self.task_id = task_id
        self.run_id = run_id
        self.parent_thread_id = PARENT
        self.agent_name = "cbam_reporting"
        self.terminal_status = "success"
        self.summary = "结果"
        self.attempts = 0


class _Result:
    def __init__(self, value: Any = None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return self._value or []


class FakeSession:
    def __init__(self, *, thread: Any, busy: bool) -> None:
        self._thread = thread
        self._busy = busy
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0

    async def scalar(self, stmt: Any) -> Any:
        sql = str(stmt)
        if "FROM thread" in sql:
            return self._thread
        if "FROM runs" in sql:
            return "some-run" if self._busy else None
        return None

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        self.updates.append((str(stmt), dict(stmt.compile().params)))
        return _Result()

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    def touched(self, needle: str) -> bool:
        """SQL 文本或绑定参数里出现过这个值（字段名或字面量都算）。"""
        return any(
            needle in sql or any(needle in str(v) for v in params.values()) for sql, params in self.updates
        )


class FakeThread:
    def __init__(self, metadata: dict[str, Any]) -> None:
        self.thread_id = PARENT
        self.user_id = "root"
        self.metadata_json = metadata


class FakeMaker:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    def __call__(self) -> FakeMaker:
        return self

    async def __aenter__(self) -> FakeSession:
        return self._session

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def patch_infra(monkeypatch: pytest.MonkeyPatch):
    def _install(session: FakeSession) -> None:
        monkeypatch.setattr(disp, "_get_session_maker", lambda: FakeMaker(session))

    return _install


def _rows() -> list[dict[str, Any]]:
    return [
        {
            "task_id": "task-a",
            "run_id": "run-a",
            "parent_thread_id": PARENT,
            "agent_name": "cbam_reporting",
            "terminal_status": "success",
            "summary": "结果",
            "attempts": 0,
        }
    ]


async def test_busy_parent_defers_without_creating_a_run(patch_infra, monkeypatch) -> None:
    """父线程有活跃 run：让给那轮的 hook（它会写 state + ack），这里不开 run。"""
    session = FakeSession(thread=FakeThread({"assistant_id": "supervisor"}), busy=True)
    patch_infra(session)

    called: list[Any] = []

    async def fake_prepare(*a: Any, **kw: Any) -> None:
        called.append(kw)

    import aegra_api.services.run_preparation as rp

    monkeypatch.setattr(rp, "_prepare_run", fake_prepare)

    await disp._dispatch_parent(PARENT, _rows())
    assert called == [], "忙的时候不许建 run"
    assert session.touched("next_attempt_at"), "应该退避后重看，而不是丢弃"


async def test_idle_parent_gets_one_wake_run_with_deterministic_id(patch_infra, monkeypatch) -> None:
    session = FakeSession(thread=FakeThread({"assistant_id": "supervisor"}), busy=False)
    patch_infra(session)

    calls: list[dict[str, Any]] = []

    async def fake_prepare(*a: Any, **kw: Any) -> None:
        calls.append(kw)

    import aegra_api.services.run_preparation as rp

    monkeypatch.setattr(rp, "_prepare_run", fake_prepare)

    await disp._dispatch_parent(PARENT, _rows())
    assert len(calls) == 1, "一批只开一轮唤醒 run"
    assert calls[0]["run_id"] == disp._wake_run_id(PARENT, _rows()), "run_id 必须是确定性的（重试幂等）"
    assert session.touched("delivered_at"), "建完 run 要把这批行标已投递"


async def test_duplicate_wake_run_id_is_treated_as_delivered(patch_infra, monkeypatch) -> None:
    """崩在「建 run 成功、标 delivered 之前」→ 重试撞主键，不能再开第二轮。"""
    session = FakeSession(thread=FakeThread({"assistant_id": "supervisor"}), busy=False)
    patch_infra(session)

    async def fake_prepare(*a: Any, **kw: Any) -> None:
        raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    import aegra_api.services.run_preparation as rp

    monkeypatch.setattr(rp, "_prepare_run", fake_prepare)

    await disp._dispatch_parent(PARENT, _rows())
    assert session.touched("delivered_at"), "撞主键说明上一轮已建过，直接标已投递"


async def test_create_failure_backs_off_and_keeps_row_pending(patch_infra, monkeypatch) -> None:
    """建 run 失败（Redis 挂了之类）→ 不丢，退避重试。"""
    session = FakeSession(thread=FakeThread({"assistant_id": "supervisor"}), busy=False)
    patch_infra(session)

    async def fake_prepare(*a: Any, **kw: Any) -> None:
        raise RuntimeError("redis down")

    import aegra_api.services.run_preparation as rp

    monkeypatch.setattr(rp, "_prepare_run", fake_prepare)

    await disp._dispatch_parent(PARENT, _rows())
    assert session.touched("last_error"), "失败要留痕"
    assert not session.touched("delivered_at"), "没投出去就不能标已投递"


async def test_missing_parent_thread_fails_permanently(patch_infra) -> None:
    session = FakeSession(thread=None, busy=False)
    patch_infra(session)
    await disp._dispatch_parent(PARENT, _rows())
    assert session.touched("'failed'") or session.touched("failed"), "父线程没了，重试无意义"


async def test_parent_without_assistant_id_fails_permanently(patch_infra) -> None:
    session = FakeSession(thread=FakeThread({}), busy=False)
    patch_infra(session)
    await disp._dispatch_parent(PARENT, _rows())
    assert session.touched("failed"), "没有 assistant_id 就建不了 run"


def test_wake_run_id_is_stable_and_order_independent() -> None:
    a, b = _rows()[0], {**_rows()[0], "task_id": "task-b", "run_id": "run-b"}
    assert disp._wake_run_id(PARENT, [a, b]) == disp._wake_run_id(PARENT, [b, a])
    assert disp._wake_run_id(PARENT, [a]) != disp._wake_run_id(PARENT, [b])


def test_backoff_grows_and_caps() -> None:
    assert disp._backoff_seconds(0) == 1.0
    assert disp._backoff_seconds(1) == 2.0
    assert disp._backoff_seconds(99) == 60.0
