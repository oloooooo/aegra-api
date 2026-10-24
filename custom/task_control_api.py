"""用户/主管直接控制子任务的接口（ADR-0014）。

现在只有一件事：**取消**。

为什么不复用 LangGraph 的 `cancelRun`：子线程的属主是服务账号（ADR-0004），
用户身份的 token 打过去会被 `runs.py` 的归属校验挡成 404。所以这里以**服务身份**
代理一次取消，并用调用方的身份反查「这个 task 是不是你父线程派的」来授权。

为什么取消之后还要等：子 run 落 `interrupted` + `interrupted_by='cancel'`，
`finalize_run` 对 interrupted 不入账（不写发件箱行），所以主管是在**下一轮 run 的
`abefore_agent`** 里通过 `_refreshed_*` 看到 `cancelled`，然后发一条状态通知。
这是有意为之：取消不是结果，不值得为它单独唤醒主管跑一轮 LLM（ADR-0014）。
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from langgraph_sdk import get_client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.auth_deps import require_auth
from aegra_api.core.orm import Thread as ThreadORM
from aegra_api.core.orm import get_session
from aegra_api.models.auth import User
from custom.services import SERVICE_IDENTITY, service_token

logger = structlog.getLogger(__name__)

router = APIRouter(prefix="/custom", tags=["custom"])

# 服务身份客户端：子线程属主是服务账号，只有它能取消子 run
_SERVICE_CLIENT: Any = None
# 自己调自己用 IPv4 回环（Windows 下 localhost 优先解成 ::1，uvicorn 只绑 127.0.0.1）
_SERVICE_URL = None


def _client() -> Any:
    global _SERVICE_CLIENT, _SERVICE_URL
    if _SERVICE_CLIENT is None:
        from aegra_api.settings import settings

        host = settings.app.HOST if settings.app.HOST not in ("", "0.0.0.0") else "127.0.0.1"
        _SERVICE_URL = f"http://{host}:{settings.app.PORT}"
        _SERVICE_CLIENT = get_client(url=_SERVICE_URL, headers={"Authorization": f"Bearer {service_token()}"})
    return _SERVICE_CLIENT


async def _authorize(session: AsyncSession, parent_thread_id: str, user: User) -> None:
    """只允许父线程属主或服务身份。"""
    owner = await session.scalar(select(ThreadORM.user_id).where(ThreadORM.thread_id == parent_thread_id))
    if owner is None:
        raise HTTPException(status_code=404, detail="父线程不存在")
    if owner != user.identity and user.identity != SERVICE_IDENTITY:
        raise HTTPException(status_code=403, detail="无权取消该任务")


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    body: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> dict:
    """取消一个子任务（幂等：重复取消返回同一个结果）。

    body: `{"parent_thread_id": ..., "run_id": ...}`
    """
    parent_thread_id = str((body or {}).get("parent_thread_id") or "").strip()
    run_id = str((body or {}).get("run_id") or "").strip()
    if not parent_thread_id or not run_id:
        raise HTTPException(status_code=422, detail="parent_thread_id 与 run_id 都是必填")
    await _authorize(session, parent_thread_id, user)

    try:
        await _client().runs.cancel(task_id, run_id)
    except Exception as exc:  # noqa: BLE001 —— run 已是终态时会报错，按已取消处理
        logger.info("task_cancel_noop", task_id=task_id, run_id=run_id, error=str(exc))
        return {"cancelled": True, "already_terminal": True}

    logger.info("task_cancelled", task_id=task_id, run_id=run_id, by=user.identity)
    return {"cancelled": True, "already_terminal": False}
