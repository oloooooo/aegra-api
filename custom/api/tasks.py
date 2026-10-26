"""异步子任务：状态查询、事件流代理、取消（ADR-0007 / ADR-0014）。

为什么不复用原生接口（见 docs/streaming.md §7）：子 agent 跑在**独立 thread** 上，属主是
服务身份 ``subagent_service``，终端用户直接 ``GET /threads/{子线程}/state`` 会被 ownership
过滤挡成 404。这里做一层**带鉴权的代理**：校验父线程归属 + 父线程 ``async_tasks`` 里确实有
该 task_id，再用服务 token 去读子线程 / 订阅子 run / 取消。服务 token 不下发给浏览器。

取消之后不额外唤醒主管：子 run 落 ``interrupted``，主管在下一轮 run 的 ``abefore_agent``
里看到 ``cancelled`` 再发状态通知（ADR-0014）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from langgraph_sdk import get_client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.auth_deps import require_auth
from aegra_api.core.orm import Thread as ThreadORM
from aegra_api.core.orm import get_session
from aegra_api.core.sse import format_sse_message, get_sse_headers
from aegra_api.models.auth import User
from custom.api.clients import _service_client, _user_client
from shared.outbox import task_status_of_run
from shared.token import SERVICE_IDENTITY, service_token

logger = structlog.getLogger(__name__)

router = APIRouter(prefix="/custom", tags=["/custom"])

_TERMINAL = frozenset({"cancelled", "success", "error", "timeout", "interrupted"})


async def _parent_state(client: Any, thread_id: str) -> dict[str, Any]:
    try:
        return (await client.threads.get_state(thread_id))["values"] or {}
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="会话不存在或无权访问") from e


async def _authorized_task(request: Request, task_id: str, parent_thread_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """校验 task 属于该用户的这个父线程，返回 (task, 父 state)。"""
    client = _user_client(request)
    state = await _parent_state(client, parent_thread_id)  # 原生接口自带 ownership 校验
    task = (state.get("async_tasks") or {}).get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="该会话下没有这个子任务")
    return task, state


async def _fresh_status(request: Request, task: dict[str, Any]) -> dict[str, Any]:
    """向子 run 核实真实状态（state 里的 status 可能停在 running，见 guard 注释）。"""
    try:
        run = await _service_client(request).runs.get(task["thread_id"], task["run_id"])
        task = {**task, "status": task_status_of_run(run) or task.get("status")}
    except Exception as e:  # noqa: BLE001 — 查不到就回原状态，不阻断前端
        logger.warning("task_status_refresh_failed", task_id=task.get("task_id"), error=str(e))
    return task


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    parent_thread_id: str,
    request: Request,
) -> dict[str, Any]:
    """单个子任务的状态（已向子 run 核实），含子 agent 目前的消息条数。"""
    task, _ = await _authorized_task(request, task_id, parent_thread_id)
    task = await _fresh_status(request, task)
    try:
        child = (await _service_client(request).threads.get_state(task["thread_id"]))["values"] or {}
        task = {**task, "message_count": len(child.get("messages") or [])}
    except Exception:  # noqa: BLE001
        task = {**task, "message_count": None}
    return task


@router.get("/tasks/{task_id}/stream")
async def stream_task(
    task_id: str,
    parent_thread_id: str,
    request: Request,
    stream_mode: str = "messages,custom",
) -> StreamingResponse:
    """把子 agent 的 run 事件转成 SSE 转发给浏览器。

    先补一帧 ``values``（子线程当前快照，覆盖连接前的输出），再实时转发；
    任务已结束则补完快照直接 ``end``。默认只转发 ``messages``+``custom``（``values``
    每步都是整态快照，量大，要就显式加）。
    """
    task, _ = await _authorized_task(request, task_id, parent_thread_id)
    modes = [m.strip() for m in stream_mode.split(",") if m.strip()]
    client = _service_client(request)

    async def gen() -> AsyncIterator[bytes]:
        yield format_sse_message("metadata", {"task_id": task_id, "thread_id": task["thread_id"]}).encode()
        try:
            snapshot = (await client.threads.get_state(task["thread_id"]))["values"] or {}
        except Exception as e:  # noqa: BLE001
            yield format_sse_message("error", {"error": "StateError", "message": str(e)}).encode()
            yield format_sse_message("end", {"status": "error"}).encode()
            return
        yield format_sse_message("values", snapshot).encode()

        status = (await _fresh_status(request, task)).get("status")
        if status in _TERMINAL:
            yield format_sse_message("end", {"status": status}).encode()
            return

        try:
            async for part in client.runs.join_stream(task["thread_id"], task["run_id"], stream_mode=modes):
                event = getattr(part, "event", "values")
                yield format_sse_message(event, getattr(part, "data", None)).encode()
                if event == "end":
                    return  # 子 run 自己发了 end，别再补一帧
        except Exception as e:  # noqa: BLE001 — 子 run 异常不能让代理崩掉
            logger.warning("task_stream_failed", task_id=task_id, error=str(e))
            yield format_sse_message("error", {"error": "StreamError", "message": str(e)}).encode()
        yield format_sse_message("end", {"status": (await _fresh_status(request, task)).get("status")}).encode()

    return StreamingResponse(gen(), headers=get_sse_headers())


def _local_service_client() -> Any:
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
        await _local_service_client().runs.cancel(task_id, run_id)
    except Exception as exc:  # noqa: BLE001 —— run 已是终态时会报错，按已取消处理
        logger.info("task_cancel_noop", task_id=task_id, run_id=run_id, error=str(exc))
        return {"cancelled": True, "already_terminal": True}

    logger.info("task_cancelled", task_id=task_id, run_id=run_id, by=user.identity)
    return {"cancelled": True, "already_terminal": False}
