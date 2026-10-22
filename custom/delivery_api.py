"""任务结果投递的 ack 接口（ADR-0010，Transactional Outbox）。

发件箱行由 `finalize_run` 在子 run 转终态时写入（主键 (task_id, run_id) 即幂等键）。
本接口只做一件事：**把这个行从 pending 原子地翻成 delivered**。

    抢到翻牌权 = 有资格投递。没抢到 = 别人已经投了（或正在投）。

为什么这就够了：翻牌是单行条件 UPDATE，本来就是原子的，不需要 advisory lock，
也不需要"多久算正在投递"的时间窗口——旧版那两个窗口（8s 让路 / 15s 过期）是在用
时间猜状态，一次 hook 里有多个待投任务时会互相挡住（三个子 agent 同时完成只投出一个）。

两个调用方：
- 主管侧的 `AsyncTaskGuard`：写进自己 run 的 state 之前先 ack；
- `custom/outbox_dispatcher.py` 直接改库（它用 SKIP LOCKED 领批，不需要走 HTTP）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.auth_deps import require_auth
from aegra_api.core.orm import Thread as ThreadORM
from aegra_api.core.orm import get_session
from aegra_api.models.auth import User
from custom.models import AsyncTaskOutbox
from custom.services import SERVICE_IDENTITY

logger = structlog.getLogger(__name__)

router = APIRouter(prefix="/custom", tags=["custom"])

# 可以抢的状态：`failed` 也放进来——分发者重试耗尽只说明"没人叫醒父线程"，
# 结果内容还在，谁把它写进 state 都比丢掉强。
_CLAIMABLE = ("pending", "failed")


async def _authorize_thread(session: AsyncSession, thread_id: str, user: User) -> None:
    """只允许父线程属主或服务身份；不认识线程直接 404。"""
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
    """把 (task_id, run_id) 的发件箱行翻成 delivered；翻成功才有资格投递。

    返回 `{"claimed": bool, "reason": ...}`：
    - `claimed`：抢到了，去投递（写 state 或开 run）；
    - `delivered`：已经被别人投了（含本进程上一次成功）；
    - `failed`：分发者重试耗尽且已被别人接管。
    """
    run_id = str(body.get("run_id") or "")
    parent_thread_id = str(body.get("parent_thread_id") or "")
    if not run_id or not parent_thread_id:
        raise HTTPException(status_code=422, detail="需要 run_id 与 parent_thread_id")

    await _authorize_thread(session, parent_thread_id, user)
    now = datetime.now(UTC)

    flipped = await session.execute(
        update(AsyncTaskOutbox)
        .where(
            AsyncTaskOutbox.task_id == task_id,
            AsyncTaskOutbox.run_id == run_id,
            AsyncTaskOutbox.status.in_(_CLAIMABLE),
        )
        .values(status="delivered", delivered_at=now, lease_expires_at=None, next_attempt_at=now)
        .returning(AsyncTaskOutbox.task_id)
    )
    if flipped.scalar_one_or_none() is not None:
        await session.commit()
        return {"claimed": True, "reason": "claimed"}

    existing = await session.scalar(
        select(AsyncTaskOutbox.status).where(
            AsyncTaskOutbox.task_id == task_id, AsyncTaskOutbox.run_id == run_id
        )
    )
    if existing is not None:
        # 已 delivered（或正被分发者租住）——谁先翻到谁投，这里放弃
        await session.commit()
        return {"claimed": False, "reason": "delivered"}

    # 行不存在：子 run 终态发生在发件箱上线之前，或这条结果不经过 finalize_run。
    # 现建一行并立刻算作抢到，保证旧数据/边缘路径仍然能投。
    inserted = await session.execute(
        pg_insert(AsyncTaskOutbox)
        .values(
            task_id=task_id,
            run_id=run_id,
            parent_thread_id=parent_thread_id,
            terminal_status="unknown",
            status="delivered",
            delivered_at=now,
        )
        .on_conflict_do_nothing(index_elements=["task_id", "run_id"])
        .returning(AsyncTaskOutbox.task_id)
    )
    await session.commit()
    if inserted.scalar_one_or_none() is None:  # 并发插入：对方赢了
        return {"claimed": False, "reason": "delivered"}
    return {"claimed": True, "reason": "inserted"}


@router.delete("/tasks/{task_id}/deliver-claim")
async def release_delivery(
    task_id: str,
    run_id: str,
    parent_thread_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> dict:
    """撤销 ack：state 没写成时调用，行退回 pending 让分发者接手。幂等。"""
    await _authorize_thread(session, parent_thread_id, user)
    await session.execute(
        update(AsyncTaskOutbox)
        .where(
            AsyncTaskOutbox.task_id == task_id,
            AsyncTaskOutbox.run_id == run_id,
            AsyncTaskOutbox.status == "delivered",
        )
        .values(status="pending", delivered_at=None)
    )
    await session.commit()
    logger.info("task_delivery_released", task_id=task_id, run_id=run_id)
    return {"released": True}
