"""多进程发件箱验证：直接打真服务（2 worker），不起 pytest。

用法：``PYTHONPATH=. PYTHONUTF8=1 .venv/Scripts/python.exe scripts/check_outbox_multiprocess.py``
前置：``docker start redis6`` + 服务端 ``--workers 2``（每进程一个分发者循环）。

验的四条：
1. 子 run 终态 → finalize_run 按子线程 metadata 写发件箱行；
2. 父线程 busy 时两个进程的分发者都不动手；
3. 转 idle 后**只**开一轮唤醒 run，行翻 delivered，再等几轮不再开第二轮；
4. guard check 命中终态 → 抢行 + ack，之后分发者不再开唤醒 run（不重复投）；
5. 8 路并发 claim 恰好一个赢家。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
import uuid

import httpx
from langgraph_sdk import get_client

from aegra_api.core.database import db_manager
from aegra_api.core.orm import Run as RunORM
from aegra_api.core.orm import Thread as ThreadORM
from aegra_api.core.orm import _get_session_maker
from aegra_api.models.enums import RunStatus  # noqa: F401 — 先加载 models，避免 run_status 的循环导入
from aegra_api.services.run_status import finalize_run
from custom.db.models import AsyncTaskOutbox
from shared.token import service_token
from graphs.middleware.async_task_guard import AsyncTaskGuard

BASE = "http://127.0.0.1:2026"
SUPERVISOR = "supervisor"
DOMAIN = "domain_agent"
OWNER = "subagent_service"
HEADERS = {"Authorization": f"Bearer {service_token()}"}

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'✓' if ok else '✗'} {name}{'' if ok else f'  ← {detail}'}")
    if not ok:
        FAILS.append(name)


async def wait(predicate, *, timeout: float = 60.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.25)
    raise AssertionError(f"timeout waiting for {what}")


async def make_thread(*, status: str = "idle", with_active_run: bool = False) -> str:
    thread_id = f"chk-outbox-{uuid.uuid4().hex[:10]}"
    maker = _get_session_maker()
    async with maker() as s:
        s.add(
            ThreadORM(
                thread_id=thread_id,
                status=status,
                metadata_json={"assistant_id": SUPERVISOR, "graph_id": SUPERVISOR},
                user_id=OWNER,
            )
        )
        await s.commit()
    if with_active_run:
        # 分发者判忙的真相是「父线程有没有 pending/running 的 run 行」，不是 thread.status
        async with maker() as s:
            s.add(
                RunORM(
                    run_id=f"parent-run-{uuid.uuid4().hex[:10]}",
                    thread_id=thread_id,
                    status="running",
                    input={},
                    user_id=OWNER,
                )
            )
            await s.commit()
    return thread_id


async def end_parent_run(thread_id: str) -> None:
    """父 run 结束：thread 转 idle + 活跃 run 行转终态（分发者才会接着投）。"""
    from sqlalchemy import update

    maker = _get_session_maker()
    async with maker() as s:
        await s.execute(update(ThreadORM).where(ThreadORM.thread_id == thread_id).values(status="idle"))
        await s.execute(
            update(RunORM)
            .where(RunORM.thread_id == thread_id, RunORM.status.in_(("pending", "running")))
            .values(status="success")
        )
        await s.commit()


async def set_status(thread_id: str, status: str) -> None:
    from sqlalchemy import update

    maker = _get_session_maker()
    async with maker() as s:
        await s.execute(update(ThreadORM).where(ThreadORM.thread_id == thread_id).values(status=status))
        await s.commit()


async def rows_of(task_id: str) -> list[dict]:
    from sqlalchemy import select

    maker = _get_session_maker()
    async with maker() as s:
        res = await s.scalars(select(AsyncTaskOutbox).where(AsyncTaskOutbox.task_id == task_id))
        return [{c.name: getattr(o, c.name) for c in AsyncTaskOutbox.__table__.columns} for o in res]


async def terminal_child(parent_id: str) -> tuple[str, str]:
    child_id = f"chk-child-{uuid.uuid4().hex[:10]}"
    run_id = f"run-{uuid.uuid4().hex[:10]}"
    maker = _get_session_maker()
    async with maker() as s:
        s.add(
            ThreadORM(
                thread_id=child_id,
                status="busy",
                metadata_json={"parent_thread_id": parent_id, "graph_id": DOMAIN, "owner_identity": OWNER},
                user_id=OWNER,
            )
        )
        await s.commit()
    # 分两次提交：runs.thread_id 有外键，同批 insert 不保证顺序
    async with maker() as s:
        s.add(RunORM(run_id=run_id, thread_id=child_id, status="running", input={}, user_id=OWNER))
        await s.commit()
    ok = await finalize_run(
        run_id,
        child_id,
        user_id=OWNER,
        status="success",
        thread_status="idle",
        output={"messages": [{"role": "ai", "content": "ok"}]},
    )
    assert ok, "finalize_run 失败"
    return child_id, run_id


async def wake_runs(thread_id: str) -> list[str]:
    from sqlalchemy import select

    maker = _get_session_maker()
    async with maker() as s:
        res = await s.scalars(select(RunORM).where(RunORM.thread_id == thread_id))
        return [r.run_id for r in res if "[Async subagent" in str((r.input or {}).get("messages") or "")]


async def is_delivered(task_id: str) -> dict | None:
    rows = await rows_of(task_id)
    return rows[0] if rows and all(r["status"] == "delivered" for r in rows) else None


async def cancel_wakes(thread_id: str) -> None:
    """唤醒 run 只需被建出来；立刻取消，不为它付一轮 LLM。"""
    client = get_client(url=BASE)
    for run_id in await wake_runs(thread_id):
        with contextlib.suppress(Exception):  # 已结束 / 已取消都无妨
            await client.runs.cancel(thread_id, run_id)


async def main() -> None:
    # 只起 SQLAlchemy engine：脚本不需要 checkpointer/store（那是服务端的事，
    # 而且 psycopg 池会和服务的连接抢额度）
    from sqlalchemy.ext.asyncio import create_async_engine

    from aegra_api.settings import settings

    db_manager.engine = create_async_engine(settings.db.database_url, pool_size=4, pool_pre_ping=True)
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get(f"{BASE}/health")
            check("服务健康（2 worker）", r.status_code == 200, r.text[:120])

        # 1. 终态入账
        parent = await make_thread()
        child, run_id = await terminal_child(parent)
        rows = await wait(lambda: rows_of(child), what="outbox row")
        row = next((r for r in rows if r["run_id"] == run_id), None)
        check("子 run 终态写发件箱行", row is not None)
        check("parent_thread_id 来自子线程 metadata", bool(row) and row["parent_thread_id"] == parent)
        check("行初始为 pending", bool(row) and row["status"] == "pending")

        # 2. busy 不投（判忙看的是父线程有没有活跃 run 行，不是 thread.status）
        busy_parent = await make_thread(status="busy", with_active_run=True)
        busy_child, _ = await terminal_child(busy_parent)
        await wait(lambda: rows_of(busy_child), what="busy child row")
        await asyncio.sleep(5)
        check("父线程 busy 时分发者不动手", await wake_runs(busy_parent) == [], str(await wake_runs(busy_parent)))

        # 2b. 父线程 interrupted（HITL 等 resume）→ 也不投：插一轮 run 会把
        #     thread.status 改成 busy，用户点“回答”时 _validate_resume_command 会 400
        hitl_parent = await make_thread(status="interrupted", with_active_run=False)
        hitl_child, _ = await terminal_child(hitl_parent)
        await wait(lambda: rows_of(hitl_child), what="interrupted child row")
        await asyncio.sleep(5)
        hitl_wakes = await wake_runs(hitl_parent)
        check("父线程 interrupted 时分发者不动手", hitl_wakes == [], str(hitl_wakes))
        check("行仍是 pending（等用户答完再投）", (await rows_of(hitl_child))[0]["status"] == "pending")
        await end_parent_run(hitl_parent)
        await wait(lambda: wake_runs(hitl_parent), what="interrupted 后放行")
        await wait(lambda: is_delivered(hitl_child), what="interrupted child delivered")
        check("interrupted 解除后才投", bool(await is_delivered(hitl_child)))
        await cancel_wakes(hitl_parent)

        # 3. 父 run 结束 → 只开一轮
        await end_parent_run(busy_parent)
        wakes = await wait(lambda: wake_runs(busy_parent), what="wake run")
        check("父线程 idle 后开唤醒 run", len(wakes) == 1, str(wakes))
        await wait(lambda: is_delivered(busy_child), what="row delivered")
        check("行翻成 delivered", bool(await is_delivered(busy_child)))
        await asyncio.sleep(5)
        again = await wake_runs(busy_parent)
        check("再等几轮 tick 也只一轮唤醒 run", len(again) == 1, str(again))
        await cancel_wakes(busy_parent)

        # 4. guard check → ack，不再唤醒。父线程先造出活跃 run，让分发者在这段时间退避，
        #    ack 完再放行，否则 1s 轮询下分发者会先抢到行（那是合法行为，不是 bug）。
        ack_parent = await make_thread(status="busy", with_active_run=True)
        ack_child, ack_run = await terminal_child(ack_parent)
        await wait(lambda: rows_of(ack_child), what="ack child row")
        # headers 必带服务 token：guard 的 _claim_async 用它认领发件箱行（缺了会 401，
        # 行停在 pending，分发者随后会多投一张卡片）
        guard = AsyncTaskGuard({DOMAIN: {"url": BASE, "graph_id": DOMAIN, "headers": HEADERS}})
        guard._parent_thread_id = lambda: ack_parent  # type: ignore[method-assign]
        tracked = {
            "task_id": ack_child,
            "agent_name": DOMAIN,
            "thread_id": ack_child,
            "run_id": ack_run,
            "status": "running",
        }

        class _Req:
            tool_call = {"name": "check_async_task", "args": {"task_id": ack_child}}
            state = {"async_tasks": {ack_child: tracked}}

        from langgraph.types import Command

        cmd = Command(update={"messages": [], "async_tasks": {ack_child: {**tracked, "status": "success"}}})
        await guard._amark_checked(_Req(), cmd)  # type: ignore[arg-type]
        check("check 命中终态记 delivered_run_id", cmd.update["async_tasks"][ack_child]["delivered_run_id"] == ack_run)
        await wait(lambda: is_delivered(ack_child), what="row acked by guard")
        check("check 命中终态同时 ack 发件箱行", bool(await is_delivered(ack_child)))
        await end_parent_run(ack_parent)
        await asyncio.sleep(5)
        check("guard 已 ack → 不再开唤醒 run", await wake_runs(ack_parent) == [], str(await wake_runs(ack_parent)))

        # 5. 并发 claim 唯一赢家。父线程必须“忙”，否则分发者会在 8 路 claim 之前就把行投掉，
        #    测到的就是“行已 delivered”而不是并发互斥。
        cc_parent = await make_thread(status="busy", with_active_run=True)
        cc_child, cc_run = await terminal_child(cc_parent)
        await wait(lambda: rows_of(cc_child), what="claim child row")

        async def claim() -> bool:
            async with httpx.AsyncClient(timeout=15) as c:
                resp = await c.post(
                    f"{BASE}/custom/tasks/{cc_child}/deliver-claim",
                    json={"parent_thread_id": cc_parent, "run_id": cc_run},
                    headers=HEADERS,
                )
                resp.raise_for_status()
                return bool(resp.json().get("claimed"))

        results = await asyncio.gather(*(claim() for _ in range(8)))
        check("8 路并发 claim 恰好一个赢家", sum(results) == 1, str(results))

        # 清理：别给后续 run 留 pending 行
        for tid, pid in ((child, parent), (cc_child, cc_parent)):
            for r in await rows_of(tid):
                if r["status"] != "delivered":
                    async with httpx.AsyncClient(timeout=10) as c:
                        await c.post(
                            f"{BASE}/custom/tasks/{tid}/deliver-claim",
                            json={"parent_thread_id": pid, "run_id": r["run_id"]},
                            headers=HEADERS,
                        )
            await cancel_wakes(pid)
    finally:
        await db_manager.engine.dispose()

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        sys.exit(1)
    print("全部通过 ✔")


if __name__ == "__main__":
    asyncio.run(main())
