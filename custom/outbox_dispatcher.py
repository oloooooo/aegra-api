"""任务结果的发件箱分发者（ADR-0010，Transactional Outbox）。

唯一职责：**在父线程空闲时，为待投递的结果开一轮唤醒 run**。

为什么要有它：子 agent 跑完时父线程可能早就答完、也没人在等它——结果得主动送过去。
以前这件事由子 agent 进程里的 `CompletionNotifierMiddleware` 顺手做，于是每个子 run
都是一个潜在写入者，判重只能靠"投递认领表 + 8s/15s 时间窗口"，多个子任务同时完成时
互相挡住会漏投。现在改成：**能开唤醒 run 的只有一个组件（本文件），它只读表、只按
状态判断**，判重退化成一次单行条件 UPDATE。

运行方式（与 `lease_reaper` / `cron_scheduler` 同款）：
- 每个进程一个 asyncio 循环，在 custom app 的 lifespan 里启停；
- 领批靠 `FOR UPDATE SKIP LOCKED` + 租约，多进程/多副本自动分工，不需要选主；
- 正本是 DB 轮询，**不依赖 Redis**。Redis 只在 `executor.submit` 那一步被用到
  （worker 模式），挂掉时行进退避重试，恢复后自动补投。

状态机：`pending` →（建出唤醒 run / 父 run 的 hook 写 state）→ `delivered`；
重试耗尽 → `failed`（`delivery_api` 仍允许 guard 把 `failed` 行抢走投递）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from langgraph_sdk import get_client
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.orm import Run as RunORM
from aegra_api.core.orm import Thread as ThreadORM
from aegra_api.core.orm import _get_session_maker
from aegra_api.models.auth import User
from aegra_api.models.runs import RunCreate
from aegra_api.services.run_status import ACTIVE_RUN_STATES
from aegra_api.settings import settings
from custom.models import AsyncTaskOutbox, UserSpace, UserTables
from custom.outbox import completion_text, is_long_result, message_text, result_body
from custom.services import service_token

logger = structlog.getLogger(__name__)

# 轮询间隔：1s 是"用户几乎察觉不到"与"空表时的 DB 压力"之间的折中。
# 多进程下每个进程各跑一个循环，空表时每条 tick 只有一次索引扫描。
_POLL_INTERVAL = float(os.getenv("OUTBOX_POLL_INTERVAL_SECONDS", "1.0"))
# 一批最多领多少行（避免一个 tick 把队列抽干，把机会让给别的进程）
_BATCH_SIZE = int(os.getenv("OUTBOX_BATCH_SIZE", "50"))
# 分发租约：领到批后多久算"我在处理"，超时说明进程死了，别人可以接手
_LEASE_SECONDS = float(os.getenv("OUTBOX_LEASE_SECONDS", "30"))
# 父线程忙时的重试间隔（不是失败，不计尝试次数）
_BUSY_BACKOFF_SECONDS = float(os.getenv("OUTBOX_BUSY_BACKOFF_SECONDS", "2"))
# 失败重试上限：超过就标 failed，交给父 run 的 hook 兜底（不永久占着轮询）
_MAX_ATTEMPTS = int(os.getenv("OUTBOX_MAX_ATTEMPTS", "10"))
# 启动延迟：等 core 的 DB / LangGraph 初始化完（lifespan 顺序不确定）
_STARTUP_DELAY_SECONDS = float(os.getenv("OUTBOX_STARTUP_DELAY_SECONDS", "5"))

# 服务身份客户端（进程级缓存）：子线程属主是服务账号，只有它能读子线程
_SERVICE_CLIENT: Any = None
# 自己调自己用 IPv4 回环：`settings.app.SERVER_URL` 推导成 localhost，
# 而 Windows 下 localhost 优先解成 ::1，uvicorn 只绑了 127.0.0.1 → 请求会挂住。
_SERVICE_URL = os.getenv("AEGRA_INTERNAL_URL") or f"http://127.0.0.1:{settings.app.PORT}"
# 读子线程摘要的超时：拿不到就退到存盘摘要，不能把整个 tick 挂死
_SUMMARY_TIMEOUT = float(os.getenv("OUTBOX_SUMMARY_TIMEOUT_SECONDS", "5"))


def _wake_run_id(parent_thread_id: str, rows: list[dict[str, Any]]) -> str:
    """唤醒 run 的确定性 ID = 幂等键。

    "建 run 成功但还没来得及标 delivered 就崩了"是最尖的一个窗口：重试时若拿到同一个
    run_id，INSERT 会撞主键 -> 我们据此认定"已经建过了"，不会给用户开第二轮。
    """
    key = f"outbox-wake:{parent_thread_id}:" + ",".join(sorted(f"{r['task_id']}/{r['run_id']}" for r in rows))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def _backoff_seconds(attempts: int) -> float:
    """指数退避，封顶 60s。"""
    return min(2.0**attempts, 60.0)


def _service_client() -> Any:
    """服务身份 SDK 客户端：只有它能读子 agent 线程（子线程属主是服务账号）。"""
    global _SERVICE_CLIENT
    if _SERVICE_CLIENT is None:
        _SERVICE_CLIENT = get_client(url=_SERVICE_URL, headers={"Authorization": f"Bearer {service_token()}"})
    return _SERVICE_CLIENT


async def _child_summary(row: dict[str, Any]) -> str:
    """结果正文 = 子线程最后一条消息（现查，**原文不截断**）。

    发件箱行**不存结果正文**（子 run 不流 `values`，存了也是空），所以只能现查。
    现查还能拿到子 agent 在 run 里后追加的内容，比写入时刻的快照准。
    """
    try:
        thread = await asyncio.wait_for(_service_client().threads.get(row["task_id"]), timeout=_SUMMARY_TIMEOUT)
        messages = ((thread or {}).get("values") or {}).get("messages") or []
        if messages:
            text = result_body(message_text(messages[-1]))
            if is_long_result(text):
                logger.warning("outbox_result_long", task_id=row["task_id"], chars=len(text))
            return text
    except Exception:  # noqa: BLE001 —— 取不到就退到存盘摘要，不阻断投递
        logger.warning("outbox_summary_fetch_failed", task_id=row["task_id"])
    return "(no output)"


async def _parent_user(session: AsyncSession, thread: ThreadORM) -> User:
    """唤醒父线程的 run 必须用**这个用户带 space 权限的身份**。

    实测后果（不带）：`domain_specs()` 拿不到 `user_spaces` → specs 为空 →
    deepagents 根本不挂 `AsyncSubAgentMiddleware`（`graph.py:917`），于是
    `start/check/list_async_task` 全部不在工具里，guard 的 `_spec_of()` 也返回 None
    → 卡片正文退化成 "(no output)"，模型的“去 check 全文”这条路也断了。

    口径与 `custom/custom_auth.py` 一致（同一张 user_space 表 + is_super）。
    """
    identity = thread.user_id
    spaces = (await session.execute(select(UserSpace.space_name).where(UserSpace.identity == identity))).scalars().all()
    is_super = await session.scalar(select(UserTables.is_super).where(UserTables.identity == identity))
    return User(
        identity=identity,
        user_spaces=sorted(spaces),
        permissions=["read", "write"] if is_super else ["read"],
    )


async def _claim_batch() -> list[dict[str, Any]]:
    """领一批待投递的行，并打上租约。多进程靠 SKIP LOCKED 分工。"""
    maker = _get_session_maker()
    now = datetime.now(UTC)
    async with maker() as session:
        stmt = (
            select(AsyncTaskOutbox)
            .where(
                AsyncTaskOutbox.status == "pending",
                AsyncTaskOutbox.next_attempt_at <= now,
                or_(AsyncTaskOutbox.lease_expires_at.is_(None), AsyncTaskOutbox.lease_expires_at < now),
            )
            .order_by(AsyncTaskOutbox.next_attempt_at.asc())
            .limit(_BATCH_SIZE)
            # shortcut: 没有单独的 claimed_by 列——同步任务里拿到行就更新租约，
            # 理论上存在"读完未更新"的极小窗口，重复只是多建一次唤醒 run。
            # 真出现并发重复时再加 claimed_by（与 runs 表同款租约字段）。
            .with_for_update(skip_locked=True)
        )
        rows = (await session.scalars(stmt)).all()
        if not rows:
            return []

        keys = [(r.task_id, r.run_id) for r in rows]
        await session.execute(
            update(AsyncTaskOutbox)
            .where(
                AsyncTaskOutbox.task_id.in_([k[0] for k in keys]),
                AsyncTaskOutbox.run_id.in_([k[1] for k in keys]),
            )
            .values(lease_expires_at=now + timedelta(seconds=_LEASE_SECONDS))
        )
        await session.commit()
        return [
            {
                "task_id": r.task_id,
                "run_id": r.run_id,
                "parent_thread_id": r.parent_thread_id,
                "agent_name": r.agent_name,
                "terminal_status": r.terminal_status,
                "attempts": r.attempts,
            }
            for r in rows
        ]


def _where_keys(rows: list[dict[str, Any]]) -> list[Any]:
    """(task_id, run_id) 的 IN 条件，供批量 UPDATE 用。"""
    ids = [r["task_id"] for r in rows]
    runs = [r["run_id"] for r in rows]
    return [
        AsyncTaskOutbox.task_id.in_(ids),
        AsyncTaskOutbox.run_id.in_(runs),
    ]


async def _settle(session: AsyncSession, rows: list[dict[str, Any]]) -> None:
    """标成已投递（成功路径）。"""
    now = datetime.now(UTC)
    await session.execute(
        update(AsyncTaskOutbox)
        .where(AsyncTaskOutbox.status == "pending", *_where_keys(rows))
        .values(status="delivered", delivered_at=now, lease_expires_at=None, next_attempt_at=now)
    )
    await session.commit()


async def _defer(session: AsyncSession, rows: list[dict[str, Any]], *, seconds: float) -> None:
    """父线程正忙：不算失败，放掉租约，稍后重看。"""
    await session.execute(
        update(AsyncTaskOutbox)
        .where(AsyncTaskOutbox.status == "pending", *_where_keys(rows))
        .values(
            lease_expires_at=None,
            next_attempt_at=datetime.now(UTC) + timedelta(seconds=seconds),
        )
    )
    await session.commit()


async def _fail(session: AsyncSession, rows: list[dict[str, Any]], *, error: str, permanent: bool = False) -> None:
    """失败：退避重试；次数耗尽或永久性错误则标 failed。"""
    now = datetime.now(UTC)
    for row in rows:
        attempts = int(row.get("attempts") or 0) + 1
        give_up = permanent or attempts >= _MAX_ATTEMPTS
        await session.execute(
            update(AsyncTaskOutbox)
            .where(
                AsyncTaskOutbox.task_id == row["task_id"],
                AsyncTaskOutbox.run_id == row["run_id"],
                AsyncTaskOutbox.status == "pending",
            )
            .values(
                attempts=attempts,
                status="failed" if give_up else "pending",
                lease_expires_at=None,
                last_error=error[:1000],
                next_attempt_at=now + timedelta(seconds=_backoff_seconds(attempts)),
            )
        )
    await session.commit()
    if any(int(r.get("attempts") or 0) + 1 >= _MAX_ATTEMPTS for r in rows):
        logger.error(
            "outbox_dispatch_gave_up",
            tasks=[f"{r['task_id']}/{r['run_id']}" for r in rows],
            error=error,
        )


async def _dispatch_parent(parent_thread_id: str, rows: list[dict[str, Any]]) -> None:
    """一个父线程一批结果：空闲就建一轮唤醒 run，忙就放回去。"""
    maker = _get_session_maker()
    async with maker() as session:
        # 锁住 thread 行：run INSERT 会对它取 FOR KEY SHARE，所以"查活跃 run → 建 run"
        # 这一段不会被别的进程插进来（与 thread_ttl 的做法一致）。
        thread = await session.scalar(
            select(ThreadORM).where(ThreadORM.thread_id == parent_thread_id).with_for_update()
        )
        if thread is None:
            await _fail(session, rows, error="parent thread missing", permanent=True)
            return

        busy = await session.scalar(
            select(RunORM.run_id)
            .where(
                RunORM.thread_id == parent_thread_id,
                RunORM.status.in_(ACTIVE_RUN_STATES),
            )
            .limit(1)
        )
        if busy is not None:
            # 活跃 run 的 before_agent 会把已终态结果写进 state 并 ack；run 期间完成的
            # 等它结束后再投（没有 after_agent），这里不抢
            await _defer(session, rows, seconds=_BUSY_BACKOFF_SECONDS)
            return

        if thread.status == "interrupted":
            # 有人在等 resume（HITL ask_user_question）。此时插一轮 run 会把
            # thread.status 从 interrupted 改成 busy，用户点“回答”时
            # `_validate_resume_command` 直接 400，整条问答链路就废了。
            # 等用户答完，那次 resume 的 before_agent 会把这些结果投出去。
            await _defer(session, rows, seconds=_BUSY_BACKOFF_SECONDS)
            return

        metadata = thread.metadata_json or {}
        assistant_id = metadata.get("assistant_id") or metadata.get("graph_id")
        if not assistant_id:
            await _fail(session, rows, error="parent thread has no assistant_id", permanent=True)
            return

        body = "\n\n".join(
            [
                completion_text(
                    r["task_id"],
                    r["run_id"],
                    r.get("agent_name"),
                    r.get("terminal_status"),
                    await _child_summary(r),
                )
                for r in sorted(rows, key=lambda r: (r.get("task_id") or "", r.get("run_id") or ""))
            ]
        )
        # 唤醒 run 必须带着用户的 space 权限（否则子 agent 工具/列表全没，见 _parent_user）
        user = await _parent_user(session, thread)
        request = RunCreate(assistant_id=str(assistant_id), input={"messages": [{"role": "user", "content": body}]})

        try:
            # 延迟导入：run_preparation 会拉起 executor / langgraph 服务，避免与 core 循环依赖
            from aegra_api.services.run_preparation import _prepare_run

            await _prepare_run(
                session,
                parent_thread_id,
                request,
                user,
                initial_status="pending",
                run_id=_wake_run_id(parent_thread_id, rows),
            )
        except IntegrityError:
            # 上一次已经建过同一轮唤醒 run（崩在标 delivered 之前）——按已投递处理
            await session.rollback()
            logger.info("outbox_wake_run_already_created", parent_thread_id=parent_thread_id)
            await _settle(session, rows)
            return
        except Exception as exc:  # noqa: BLE001 —— 任何失败都要退避重试，不能丢结果
            await session.rollback()
            logger.warning("outbox_dispatch_failed", parent_thread_id=parent_thread_id, error=str(exc), exc_info=True)
            await _fail(session, rows, error=str(exc))
            return

        await _settle(session, rows)
        logger.info(
            "outbox_wake_run_created",
            parent_thread_id=parent_thread_id,
            tasks=[f"{r['task_id']}/{r['run_id']}" for r in rows],
        )


async def dispatch_once() -> int:
    """跑一轮分发，返回领到的行数（0 表示没事做）。"""
    rows = await _claim_batch()
    if not rows:
        return 0
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["parent_thread_id"]].append(row)
    for parent, group in grouped.items():
        try:
            await _dispatch_parent(parent, group)
        except Exception:  # noqa: BLE001 —— 一个父线程出错不影响其它父线程
            logger.exception("outbox_dispatch_parent_error", parent_thread_id=parent)
    return len(rows)


class OutboxDispatcher:
    """进程内后台循环；与 lease_reaper / cron_scheduler 同款启停。"""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Outbox dispatcher started", interval_seconds=_POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Outbox dispatcher stopped")

    async def _loop(self) -> None:
        """先等 core 初始化完，再 tick；sleep 放在 finally，DB 挂了也不会空转。"""
        try:
            await asyncio.sleep(_STARTUP_DELAY_SECONDS)
        except asyncio.CancelledError:
            return
        while self._running:
            try:
                await dispatch_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("outbox dispatcher tick failed")
            try:
                await asyncio.sleep(_POLL_INTERVAL)
            except asyncio.CancelledError:
                break


dispatcher = OutboxDispatcher()
