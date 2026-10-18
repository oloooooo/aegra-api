"""任务结果投递的认领接口（ADR-0010）。

跨进程、跨 run 的判重不能靠内存，也不该为了判重去读整份 thread state
（`GET /threads/{id}/state` 会把全部 messages 拉回来）。这里用一张两列主键的表当
幂等键：谁抢到认领，谁才有资格投递（注入消息 or 开投递 run）。

两个调用方：
- 主管侧的 `AsyncTaskGuard`：准备把结果注入当前 run 之前先认领；
- 子 agent 侧的 `CompletionNotifierMiddleware`：准备开新 run 之前先认领。

并发让路：同一父线程上，若数十秒内已有别的任务认领过（说明它的投递 run 正在路上），
后到者直接放弃——主管的下一个边界会通过注入拿到结果，不需要再开一个并发 run。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.auth_deps import require_auth
from aegra_api.core.orm import Thread as ThreadORM
from aegra_api.core.orm import get_session
from aegra_api.models.auth import User
from custom.models import AsyncTaskDelivery
from custom.services import SERVICE_IDENTITY

logger = structlog.getLogger(__name__)

router = APIRouter(prefix="/custom", tags=["custom"])

# 认领有效期：
# - 同线程让路窗口（`_IN_FLIGHT_SECONDS`）：刚认领的任务说明它的投递 run 正在创建，
#   此时再开第二个会并发写同一线程；
# - 过期重投（`_STALE_SECONDS`）：认领后进程崩溃导致没真投出去，过了这个时间允许重投。
#
# 不变式：`_STALE_SECONDS` 必须大于「一次正常投递可能占用的全部时间」——即 notifier 的
# 重试（0.5+1+1.5s）+ 延迟复查（5+10+20s，见 completion_notifier._RECHECK_DELAYS）
# ≈ 38s。否则同一任务会被正常投完一次之后又因"过期"再投一次。
# 真正的投递动作（插认领 + 建 run）是毫秒级，90s 已留足余量；崩溃场景下最多损失 90s。
_IN_FLIGHT_SECONDS = 8
_STALE_SECONDS = 90


async def _authorize_thread(session: AsyncSession, thread_id: str, user: User) -> None:
    """认领只允许父线程属主或服务身份；不认识线程直接 404。"""
    owner = await session.scalar(select(ThreadORM.user_id).where(ThreadORM.thread_id == thread_id))
    if owner is None:
        raise HTTPException(status_code=404, detail="父线程不存在")
    if owner != user.identity and user.identity != SERVICE_IDENTITY:
        raise HTTPException(status_code=403, detail="无权为该线程认领任务投递")


@router.post("/tasks/{task_id}/deliver-claim")
async def claim_delivery(
    task_id: str,
    body: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> dict:
    """认领 (task_id, run_id) 的投递权。

    返回 `{"claimed": bool, "reason": ...}`：
    - `claimed`：已认领，调用方可以去投递（注入或开 run）；
    - `delivered`：这次执行的结果已经投过；
    - `in_flight`：同线程上刚刚有别的任务认领，让路（主管边界注入会兜住）。
    """
    run_id = str(body.get("run_id") or "")
    parent_thread_id = str(body.get("parent_thread_id") or "")
    if not run_id or not parent_thread_id:
        raise HTTPException(status_code=422, detail="需要 run_id 与 parent_thread_id")

    await _authorize_thread(session, parent_thread_id, user)
    now = datetime.now(UTC)

    # 事务级 advisory lock：同一父线程的认领串行，跨进程也生效
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"task-delivery:{parent_thread_id}"}
    )
    existing = await session.scalar(
        select(AsyncTaskDelivery).where(
            AsyncTaskDelivery.task_id == task_id, AsyncTaskDelivery.run_id == run_id
        )
    )
    if existing is not None:
        age = now - existing.claimed_at if existing.claimed_at else timedelta(seconds=0)
        if age < timedelta(seconds=_STALE_SECONDS):
            await session.commit()
            return {"claimed": False, "reason": "delivered"}
        # 认领过期（认领后进程死掉、没真投出去）→ 刷新时间后重新放行
        existing.claimed_at = now
        await session.commit()
        logger.info("task_delivery_claim_reclaimed", task_id=task_id, run_id=run_id)
        return {"claimed": True, "reason": "reclaimed"}

    in_flight = await session.scalar(
        select(AsyncTaskDelivery.task_id)
        .where(
            AsyncTaskDelivery.parent_thread_id == parent_thread_id,
            AsyncTaskDelivery.claimed_at > now - timedelta(seconds=_IN_FLIGHT_SECONDS),
        )
        .limit(1)
    )
    if in_flight is not None:
        await session.commit()
        return {"claimed": False, "reason": "in_flight"}

    await session.execute(
        pg_insert(AsyncTaskDelivery)
        .values(task_id=task_id, run_id=run_id, parent_thread_id=parent_thread_id, claimed_at=now)
        .on_conflict_do_nothing()
    )
    await session.commit()
    logger.info("task_delivery_claimed", task_id=task_id, run_id=run_id, parent_thread_id=parent_thread_id)
    return {"claimed": True, "reason": "claimed"}


@router.delete("/tasks/{task_id}/deliver-claim")
async def release_delivery(
    task_id: str,
    run_id: str,
    parent_thread_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> dict:
    """撤销认领（投递 run 创建失败时调用），让后续重试还能投。幂等。"""
    await _authorize_thread(session, parent_thread_id, user)
    await session.execute(
        delete(AsyncTaskDelivery).where(
            AsyncTaskDelivery.task_id == task_id,
            AsyncTaskDelivery.run_id == run_id,
            AsyncTaskDelivery.parent_thread_id == parent_thread_id,
        )
    )
    await session.commit()
    logger.info("task_delivery_claim_released", task_id=task_id, run_id=run_id)
    return {"released": True}
