"""拦截 start/update_async_task：转发 spaces + 并发上限（ADR-0005、Q2/Q5 决议）。

deepagents 内置的 start/update 工具创建子 run 时不带任何 config（AsyncSubAgent
spec 没有 config 字段，内置工具也没有关闭开关），因此这里短路 handler，自己用
spec 里的静态服务 token 建 thread/run，并附上：

- ``config.configurable.user_spaces``：主管此刻的 spaces（取自主管自己的
  ``langgraph_auth_user``，服务端注入、客户端伪造不了）；主管无鉴权上下文
  （本地 demo）时透传 null，子侧解析为不设限。
- 并发上限：非终态任务 ≥ ``MAX_ACTIVE_TASKS``（3）时拒绝，返回错误文本让模型
  自己等待/重试（Q3a：普通错误，不走 HITL）。

check/cancel/list 不创建 run，直接放行给内置 handler。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from langgraph_sdk import get_client, get_sync_client
from langgraph_sdk.client import LangGraphClient, SyncLangGraphClient

from graphs.space_access import allowed_spaces

logger = structlog.getLogger(__name__)

MAX_ACTIVE_TASKS = 3

_TERMINAL = frozenset({"cancelled", "success", "error", "timeout", "interrupted"})


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class AsyncTaskGuard(AgentMiddleware):
    """拦截 async subagent 的 run 创建，注入 user_spaces 并限制并发。"""

    def __init__(self, specs: dict[str, dict[str, Any]]) -> None:
        """specs: {agent_name: AsyncSubAgent spec}（url/headers/graph_id 都在其中）。"""
        self._specs = specs
        self._sync: dict[tuple, SyncLangGraphClient] = {}
        self._async: dict[tuple, LangGraphClient] = {}

    # ---------- 客户端（按 url+headers 缓存，与 deepagents _ClientCache 同键） ----------

    @staticmethod
    def _key(spec: dict[str, Any]) -> tuple:
        return (spec.get("url"), frozenset((spec.get("headers") or {}).items()))

    def _sync_client(self, spec: dict[str, Any]) -> SyncLangGraphClient:
        key = self._key(spec)
        if key not in self._sync:
            self._sync[key] = get_sync_client(url=spec["url"], headers=dict(spec.get("headers") or {}))
        return self._sync[key]

    def _async_client(self, spec: dict[str, Any]) -> LangGraphClient:
        key = self._key(spec)
        if key not in self._async:
            self._async[key] = get_client(url=spec["url"], headers=dict(spec.get("headers") or {}))
        return self._async[key]

    # ---------- 共享判定 ----------

    def _validate(self, agent_type: str) -> str | None:
        if agent_type not in self._specs:
            allowed = ", ".join(f"`{k}`" for k in self._specs)
            return f"Unknown async subagent type `{agent_type}`. Available types: {allowed}"
        return None

    @staticmethod
    def _active_count(state: Any) -> int:
        tasks: dict[str, Any] = (state or {}).get("async_tasks") or {}
        return sum(1 for t in tasks.values() if t.get("status") not in _TERMINAL)

    @staticmethod
    def _spaces_payload(config: Any) -> list[str] | None:
        """主管当前 spaces：集合→list，None（无鉴权上下文）→None（透传 null）。"""
        spaces = allowed_spaces(config)
        return None if spaces is None else sorted(spaces)

    @staticmethod
    def _over_limit(state: Any) -> str:
        return (
            f"Too many active async tasks ({MAX_ACTIVE_TASKS}). "
            "Wait for one to finish or cancel it before launching another."
        )

    def _start_args(self, request: ToolCallRequest) -> tuple[dict[str, Any] | None, str | None]:
        """返回 (spec, error_msg)。"""
        args = request.tool_call.get("args") or {}
        agent_type = args.get("subagent_type", "")
        if err := self._validate(agent_type):
            return None, err
        if self._active_count(request.state) >= MAX_ACTIVE_TASKS:
            return None, self._over_limit(request.state)
        return self._specs[agent_type], None

    # ---------- start ----------

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Any
    ) -> ToolMessage | Command[Any]:
        name = request.tool_call.get("name")
        if name == "start_async_task":
            return self._start_sync(request)
        if name == "update_async_task":
            return self._update_sync(request)
        return handler(request)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Any
    ) -> ToolMessage | Command[Any]:
        name = request.tool_call.get("name")
        if name == "start_async_task":
            return await self._astart(request)
        if name == "update_async_task":
            return await self._aupdate(request)
        return await handler(request)

    def _err(self, request: ToolCallRequest, msg: str) -> ToolMessage:
        return ToolMessage(content=msg, tool_call_id=request.runtime.tool_call_id, name=request.tool_call.get("name"))

    def _config_for(self, request: ToolCallRequest) -> dict[str, Any]:
        return {"configurable": {"user_spaces": self._spaces_payload(request.runtime.config)}}

    def _task(self, task_id: str, agent_name: str, run_id: str, created_at: str | None = None) -> dict[str, Any]:
        now = _now()
        return {
            "task_id": task_id,
            "agent_name": agent_name,
            "thread_id": task_id,
            "run_id": run_id,
            "status": "running",
            "created_at": created_at or now,
            "last_checked_at": now,
            "last_updated_at": now,
        }

    def _launched(self, request: ToolCallRequest, task: dict[str, Any]) -> Command[Any]:
        msg = f"Launched async subagent. task_id: {task['task_id']}"
        return Command(
            update={
                "messages": [ToolMessage(msg, tool_call_id=request.runtime.tool_call_id)],
                "async_tasks": {task["task_id"]: task},
            }
        )

    def _start_sync(self, request: ToolCallRequest) -> ToolMessage | Command[Any]:
        spec, err = self._start_args(request)
        if err:
            return self._err(request, err)
        assert spec is not None
        args = request.tool_call.get("args") or {}
        client = self._sync_client(spec)
        try:
            thread = client.threads.create()
            run = client.runs.create(
                thread_id=thread["thread_id"],
                assistant_id=spec["graph_id"],
                input={"messages": [{"role": "user", "content": args.get("description", "")}]},
                config=self._config_for(request),
            )
        except Exception as e:  # noqa: BLE001 — SDK 异常无类型，回给模型
            logger.warning("failed_to_launch_async_subagent", error=str(e))
            return self._err(request, f"Failed to launch async subagent: {e}")
        return self._launched(request, self._task(thread["thread_id"], spec_name(spec, self._specs), run["run_id"]))

    async def _astart(self, request: ToolCallRequest) -> ToolMessage | Command[Any]:
        spec, err = self._start_args(request)
        if err:
            return self._err(request, err)
        assert spec is not None
        args = request.tool_call.get("args") or {}
        client = self._async_client(spec)
        try:
            thread = await client.threads.create()
            run = await client.runs.create(
                thread_id=thread["thread_id"],
                assistant_id=spec["graph_id"],
                input={"messages": [{"role": "user", "content": args.get("description", "")}]},
                config=self._config_for(request),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("failed_to_launch_async_subagent", error=str(e))
            return self._err(request, f"Failed to launch async subagent: {e}")
        return self._launched(request, self._task(thread["thread_id"], spec_name(spec, self._specs), run["run_id"]))

    # ---------- update（新 run 同样要带 config，否则子 agent 中途丢 spaces） ----------

    def _tracked(self, request: ToolCallRequest, task_id: str) -> dict[str, Any] | str:
        tasks: dict[str, Any] = (request.state or {}).get("async_tasks") or {}
        tracked = tasks.get((task_id or "").strip())
        if not tracked:
            return f"No tracked task found for task_id: {task_id!r}"
        return tracked

    def _updated(self, request: ToolCallRequest, tracked: dict[str, Any], run_id: str) -> Command[Any]:
        task = {**tracked, "run_id": run_id, "status": "running", "last_updated_at": _now()}
        msg = f"Updated async subagent. task_id: {tracked['task_id']}"
        return Command(
            update={
                "messages": [ToolMessage(msg, tool_call_id=request.runtime.tool_call_id)],
                "async_tasks": {tracked["task_id"]: task},
            }
        )

    def _update_sync(self, request: ToolCallRequest) -> ToolMessage | Command[Any]:
        tracked = self._tracked(request, (request.tool_call.get("args") or {}).get("task_id", ""))
        if isinstance(tracked, str):
            return self._err(request, tracked)
        spec = self._specs.get(tracked["agent_name"])
        if spec is None:
            return self._err(request, f"Unknown agent for task: {tracked['task_id']!r}")
        client = self._sync_client(spec)
        try:
            run = client.runs.create(
                thread_id=tracked["thread_id"],
                assistant_id=spec["graph_id"],
                input={"messages": [{"role": "user", "content": (request.tool_call.get("args") or {}).get("message", "")}]},
                multitask_strategy="interrupt",
                config=self._config_for(request),
            )
        except Exception as e:  # noqa: BLE001
            return self._err(request, f"Failed to update async subagent: {e}")
        return self._updated(request, tracked, run["run_id"])

    async def _aupdate(self, request: ToolCallRequest) -> ToolMessage | Command[Any]:
        tracked = self._tracked(request, (request.tool_call.get("args") or {}).get("task_id", ""))
        if isinstance(tracked, str):
            return self._err(request, tracked)
        spec = self._specs.get(tracked["agent_name"])
        if spec is None:
            return self._err(request, f"Unknown agent for task: {tracked['task_id']!r}")
        client = self._async_client(spec)
        try:
            run = await client.runs.create(
                thread_id=tracked["thread_id"],
                assistant_id=spec["graph_id"],
                input={"messages": [{"role": "user", "content": (request.tool_call.get("args") or {}).get("message", "")}]},
                multitask_strategy="interrupt",
                config=self._config_for(request),
            )
        except Exception as e:  # noqa: BLE001
            return self._err(request, f"Failed to update async subagent: {e}")
        return self._updated(request, tracked, run["run_id"])


def spec_name(spec: dict[str, Any], specs: dict[str, dict[str, Any]]) -> str:
    """反查 spec 对应的 agent 名（task.agent_name 需要）。"""
    for name, s in specs.items():
        if s is spec:
            return name
    return str(spec.get("name", ""))
