"""Run and thread status management.

Provides the database-level status update operations used by both the
API layer (cancel, interrupt) and the execution layer (run_executor,
worker_executor). Extracted from api/runs.py to eliminate the circular
dependency where service code imported from the API module.
"""

from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.orm import Run as RunORM
from aegra_api.core.orm import Thread as ThreadORM
from aegra_api.core.orm import _get_session_maker
from aegra_api.core.serializers import GeneralSerializer
from aegra_api.utils.status_compat import validate_run_status, validate_thread_status
from custom.models import AsyncTaskOutbox
from custom.outbox import summary_from_output

logger = structlog.getLogger(__name__)
_serializer = GeneralSerializer()
ACTIVE_RUN_STATES = ("pending", "running")


async def start_run(run_id: str, *, user_id: str) -> bool:
    """Move an active run to running without reviving a terminal run."""
    maker = _get_session_maker()
    async with maker() as session:
        result = await session.execute(
            update(RunORM)
            .where(
                RunORM.run_id == run_id,
                RunORM.user_id == user_id,
                RunORM.status.in_(ACTIVE_RUN_STATES),
            )
            .values(status="running", updated_at=datetime.now(UTC))
            .returning(RunORM.run_id)
        )
        started = result.scalar_one_or_none() is not None
        if started:
            await session.commit()
        else:
            await session.rollback()
        return started


async def set_thread_status(session: AsyncSession, thread_id: str, status: str) -> None:
    """Update a thread's status column.

    Does NOT commit — the caller controls the transaction boundary.
    This allows thread status and run updates to share a single commit.
    """
    validated = validate_thread_status(status)
    result = cast(
        CursorResult,
        await session.execute(
            update(ThreadORM)
            .where(ThreadORM.thread_id == thread_id)
            .values(status=validated, updated_at=datetime.now(UTC))
        ),
    )
    if result.rowcount == 0:
        raise ValueError(f"Thread '{thread_id}' not found")


async def set_thread_status_if_no_active_runs(
    session: AsyncSession,
    thread_ids: Collection[str],
    status: str,
    *,
    user_id: str,
) -> None:
    """Update threads that no longer have a pending or running run.

    Does not commit so callers can keep the run and thread transitions in
    one transaction.
    """
    if not thread_ids:
        return

    validated = validate_thread_status(status)
    active_run_exists = exists(
        select(RunORM.run_id)
        .where(
            RunORM.thread_id == ThreadORM.thread_id,
            RunORM.user_id == user_id,
            RunORM.status.in_(ACTIVE_RUN_STATES),
        )
        .correlate(ThreadORM)
    )
    await session.execute(
        update(ThreadORM)
        .where(
            ThreadORM.thread_id.in_(thread_ids),
            ThreadORM.user_id == user_id,
            ~active_run_exists,
        )
        .values(status=validated, updated_at=datetime.now(UTC))
    )


async def interrupt_unowned_run(
    session: AsyncSession,
    run_id: str,
    thread_id: str,
    *,
    user_id: str,
) -> bool:
    """Interrupt an active run only when it has no live database owner.

    The ownership predicate is checked in the UPDATE so a worker that renews
    or claims the run concurrently cannot be overwritten by the API process.
    If a live worker merely missed its lease, the caller asks it to stop through
    the broker, and guarded finalization rejects any late worker write.

    Attribution is always ``interrupted_by='cancel'``: this path only fires
    from explicit cancellation (client cancel or Supersede), never from HITL.
    """
    now = datetime.now(UTC)
    result = cast(
        CursorResult,
        await session.execute(
            update(RunORM)
            .where(
                RunORM.run_id == run_id,
                RunORM.thread_id == thread_id,
                RunORM.user_id == user_id,
                RunORM.status.in_(ACTIVE_RUN_STATES),
                or_(
                    RunORM.claimed_by.is_(None),
                    RunORM.lease_expires_at < now,
                ),
            )
            .values(
                status="interrupted",
                interrupted_by="cancel",
                claimed_by=None,
                lease_expires_at=None,
                updated_at=now,
            )
            .returning(RunORM.run_id)
        ),
    )
    if result.scalar_one_or_none() is None:
        return False

    await set_thread_status_if_no_active_runs(session, [thread_id], "idle", user_id=user_id)
    await session.commit()
    logger.info("Interrupted unowned run", run_id=run_id, thread_id=thread_id)
    return True


async def finalize_run(
    run_id: str,
    thread_id: str,
    *,
    user_id: str,
    status: str,
    thread_status: str,
    output: Any = None,
    error: str | None = None,
    interrupted_by: str | None = None,
) -> bool:
    """Conditionally update run and thread status in one transaction.

    Returns false when another actor has already made the run terminal. This
    prevents an expired worker from overwriting a reconciled cancellation.

    ``interrupted_by`` attributes an ``interrupted`` status to its cause
    ('cancel' or 'hitl'); pass None for normal terminal states.
    """
    validated_run = validate_run_status(status)
    validated_thread = validate_thread_status(thread_status)
    maker = _get_session_maker()

    run_values: dict[str, Any] = {
        "status": validated_run,
        "updated_at": datetime.now(UTC),
    }
    if output is not None:
        run_values["output"] = _safe_serialize(output, run_id)
    if error is not None:
        run_values["error_message"] = error
    if interrupted_by is not None:
        run_values["interrupted_by"] = interrupted_by

    async with maker() as session:
        result = await session.execute(
            update(RunORM)
            .where(
                RunORM.run_id == run_id,
                RunORM.user_id == user_id,
                RunORM.status.in_(ACTIVE_RUN_STATES),
            )
            .values(**run_values)
            .returning(RunORM.run_id)
        )
        if result.scalar_one_or_none() is None:
            await session.rollback()
            logger.info("Skipped finalizing terminal run", run_id=run_id, status=validated_run)
            return False

        await set_thread_status_if_no_active_runs(
            session,
            [thread_id],
            validated_thread,
            user_id=user_id,
        )
        await _enqueue_task_result(
            session, run_id=run_id, thread_id=thread_id, status=validated_run, output=output
        )
        await session.commit()

    logger.info("Finalized run", run_id=run_id, status=validated_run, thread_status=validated_thread)
    return True


async def _enqueue_task_result(
    session: AsyncSession,
    *,
    run_id: str,
    thread_id: str,
    status: str,
    output: Any,
) -> None:
    """把终态结果写进发件箱（ADR-0010），供分发者唤醒父线程。

    识别方式：子线程的 metadata 里有 `parent_thread_id`（派活时 `AsyncTaskGuard` 写入的
    归属标记），而主管自己的线程没有这个字段。

    - 同一个事务：子 run 转终态与发件箱插入一起提交，不会出现“状态改了但结果没入账”；
    - 幂等：worker lease 过期重跑会再次进入这里，主键 (task_id, run_id) 挡住重复。

    `interrupted` 不入账：那意味着子 run 被取消或被 `update_async_task` 顶掉，这一轮的
    结果并不完整，后续那次 run 会自己入账。
    """
    if status == "interrupted":
        return

    metadata = await session.scalar(select(ThreadORM.metadata_json).where(ThreadORM.thread_id == thread_id))
    if not isinstance(metadata, dict):
        return  # 没元数据 / 不是对象形态：识别不出父子关系，不入账
    parent_thread_id = metadata.get("parent_thread_id")
    if not parent_thread_id:
        return  # 主管自己的 run，没人等它

    await session.execute(
        pg_insert(AsyncTaskOutbox)
        .values(
            task_id=thread_id,
            run_id=run_id,
            parent_thread_id=str(parent_thread_id),
            agent_name=metadata.get("graph_id"),
            terminal_status=status,
            summary=summary_from_output(output),
        )
        .on_conflict_do_nothing(index_elements=["task_id", "run_id"])
    )


def _safe_serialize(output: Any, run_id: str) -> Any:
    """Serialize output with a fallback for non-JSON-compatible objects."""
    try:
        return _serializer.serialize(output)
    except Exception as exc:
        logger.warning("Output serialization failed", run_id=run_id, error=str(exc))
        return {
            "error": "Output serialization failed",
            "original_type": str(type(output)),
        }
