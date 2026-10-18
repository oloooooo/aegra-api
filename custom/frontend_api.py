"""前端专用接口：图清单、知识空间浏览、异步子任务状态与实时流。

为什么不直接复用 LangGraph 原生接口（见 docs/streaming.md §7）：

- ``GET /assistants`` 会把所有历史 assistant 都吐出来（含已删除的图和示例图），
  前端需要的只是 aegra.json 里注册的那几个，所以给一个 ``/custom/graphs``。
- 异步子 agent 跑在**独立 thread** 上，属主是服务身份 ``subagent_service``，
  终端用户直接 ``GET /threads/{子线程}/state`` 会被 ownership 过滤挡成 404。
  这里做一层**带鉴权的代理**：校验父线程归属 + 父线程 ``async_tasks`` 里确实有
  该 task_id，再用服务 token 去读子线程 / 订阅子 run 的事件流。

鉴权模型：调用方必须带自己的 JWT（``require_auth``）；服务 token 只在服务端内部
使用，不下发给浏览器。
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from langgraph_sdk import get_client
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.auth_deps import require_auth
from aegra_api.core.orm import get_session
from aegra_api.core.sse import format_sse_message, get_sse_headers
from aegra_api.models.auth import User
from custom.repositories import SpaceRepository, UserSpaceRepository
from custom.services import service_token

logger = structlog.getLogger(__name__)

router = APIRouter(prefix="/custom", tags=["/custom"])

_TERMINAL = frozenset({"cancelled", "success", "error", "timeout", "interrupted"})
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_GRAPH_IDS_CACHE: dict[str, tuple[int, list[str]]] = {}

# 客户端缓存：同一 (base_url, token) 复用，避免每请求新建连接池。
# shortcut: 超过上限时丢弃最旧条目但不显式 aclose()，连接由 GC 回收；
#           并发用户数远大于 8 时才需要改成带关闭的 LRU。
_CLIENTS: dict[tuple[str, str], Any] = {}
_CLIENT_CACHE_MAX = 8


# ---------- 工具 ----------


def _graph_ids() -> list[str]:
    """aegra.json 里注册的图（启动时读的那份配置）。

    按 mtime 缓存：配置改了（通常需重启）不必每请求重读；配置坏了要留痕，
    不能静默变成空列表——前端会以为「没有可用助手」。
    """
    cfg = _PROJECT_ROOT / os.environ.get("AEGRA_CONFIG", "aegra.json")
    try:
        mtime = cfg.stat().st_mtime_ns
    except OSError:
        logger.warning("aegra_config_missing", path=str(cfg))
        return []
    cached = _GRAPH_IDS_CACHE.get("v")
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("aegra_config_unreadable", path=str(cfg), error=str(e))
        return []
    graphs = data.get("graphs")
    if not isinstance(graphs, dict) or not graphs:
        logger.warning("aegra_config_no_graphs", path=str(cfg), keys=sorted(data))
        return []
    ids = sorted(graphs)
    _GRAPH_IDS_CACHE["v"] = (mtime, ids)
    return ids


def _client(request: Request, token: str) -> Any:
    """按 (base_url, token) 复用 SDK 客户端；token 相同即同一身份，可安全共享。"""
    key = (str(request.base_url).rstrip("/"), token)
    client = _CLIENTS.get(key)
    if client is None:
        if len(_CLIENTS) >= _CLIENT_CACHE_MAX:
            _CLIENTS.pop(next(iter(_CLIENTS)))
        client = get_client(url=key[0], headers={"Authorization": f"Bearer {token}"})
        _CLIENTS[key] = client
    return client


def _service_client(request: Request) -> Any:
    """服务身份客户端（能读子 agent 的线程）。"""
    return _client(request, service_token())


def _user_client(request: Request) -> Any:
    """用户身份客户端：借它走原生接口，顺带享受 ownership 校验。"""
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="缺少令牌")
    return _client(request, token)


async def _own_space_names(session: AsyncSession, user: User) -> set[str]:
    if "write" in (user.permissions or []):
        return {s.name for s in await SpaceRepository(session).list_all()}
    return set(await UserSpaceRepository(session).list_by_user(user.identity))


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
        task = {**task, "status": run.get("status") or task.get("status")}
    except Exception as e:  # noqa: BLE001 — 查不到就回原状态，不阻断前端
        logger.warning("task_status_refresh_failed", task_id=task.get("task_id"), error=str(e))
    return task


# ---------- 图清单 ----------


@router.get("/graphs")
async def list_graphs(user: Annotated[User, Depends(require_auth)]) -> dict[str, Any]:
    """前端可选的助手：只列 aegra.json 注册的图，不含历史/示例 assistant。"""
    return {"graphs": _graph_ids(), "identity": user.identity, "permissions": user.permissions}


# ---------- 知识空间 ----------


@router.get("/spaces")
async def list_spaces(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> dict[str, Any]:
    """全部空间 + 当前用户是否有权限 + 文件数。"""
    spaces = await SpaceRepository(session).list_all()
    mine = set(await UserSpaceRepository(session).list_by_user(user.identity))
    is_super = "write" in (user.permissions or [])
    out = []
    for s in spaces:
        root = _PROJECT_ROOT / s.path
        files = sum(1 for p in root.rglob("*") if p.is_file()) if root.is_dir() else 0
        out.append(
            {
                "name": s.name,
                "path": s.path,
                "accessible": is_super or s.name in mine,
                "file_count": files,
            }
        )
    return {"spaces": out, "is_super": is_super}


@router.get("/spaces/{space_name}/files")
async def list_space_files(
    space_name: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> dict[str, Any]:
    """空间内文件树（只读）。递归列出相对路径、大小、修改时间。"""
    if space_name not in await _own_space_names(session, user):
        raise HTTPException(status_code=403, detail=f"无权访问空间 {space_name}")
    space = await SpaceRepository(session).get(space_name)
    if space is None:
        raise HTTPException(status_code=404, detail="空间不存在")
    root = _PROJECT_ROOT / space.path
    if not root.is_dir():
        return {"space": space_name, "root": space.path, "files": []}
    files = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        st = p.stat()
        files.append(
            {
                "path": "/" + p.relative_to(root).as_posix(),
                "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
                "ext": p.suffix.lower(),
            }
        )
    return {"space": space_name, "root": space.path, "files": files}


@router.get("/spaces/{space_name}/users")
async def list_space_users(
    space_name: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> dict[str, Any]:
    """空间成员（超级用户可看全部，普通用户只看自己所在空间）。"""
    if space_name not in await _own_space_names(session, user):
        raise HTTPException(status_code=403, detail=f"无权访问空间 {space_name}")
    rows = await UserSpaceRepository(session).list_by_space(space_name)
    return {"space": space_name, "users": [r.identity for r in rows]}


@router.delete("/spaces/{space_name}/users/{identity}")
async def remove_space_user(
    space_name: str,
    identity: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> dict[str, Any]:
    """把用户移出空间（仅超级用户）。"""
    if "write" not in (user.permissions or []):
        raise HTTPException(status_code=403, detail="只有超级用户可以分配空间")
    ok = await UserSpaceRepository(session).delete(identity, space_name)
    if not ok:
        raise HTTPException(status_code=404, detail="该用户不在这个空间里")
    return {"space": space_name, "removed": identity}


# ---------- 异步子任务 ----------


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
