"""拦截 start/update_async_task：转发 spaces + 并发上限（ADR-0005、Q2/Q5 决议）。

deepagents 内置的 start/update 工具创建子 run 时不带任何 config（AsyncSubAgent
spec 没有 config 字段，内置工具也没有关闭开关），因此这里短路 handler，自己用
spec 里的静态服务 token 建 thread/run，并附上：

- ``config.configurable.user_spaces``：spec 绑定了 ``space`` 的子 agent 只转发
  ``[space]``（仅本任务 space 收紧），且先校验主管此刻的 spaces（取自主管自己的
  ``langgraph_auth_user``，服务端注入、客户端伪造不了）含该空间，否则拒绝启动；
  spec 无 space 绑定则回退全量快照；主管无鉴权上下文（本地 demo）时透传 null，
  子侧解析为不设限。
- 并发上限：非终态任务 ≥ ``MAX_ACTIVE_TASKS``（3）时拒绝，返回错误文本让模型
  自己等待/重试（Q3a：普通错误，不走 HITL）。**判前先向子 run 核实真实状态**：
  回写路径（CompletionNotifier → 唤醒主管）不经过 check_async_task，state 里的
  ``status`` 会停在 ``running``，不核实就会把 3 个已成功的任务当活跃，永久卡死名额。

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

from graphs.space_access import allowed_spaces, current_identity

logger = structlog.getLogger(__name__)

MAX_ACTIVE_TASKS = 3

_TERMINAL = frozenset({"cancelled", "success", "error", "timeout", "interrupted"})


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class AsyncTaskGuard(AgentMiddleware):
    """拦截 async subagent 的 run 创建，注入 user_spaces 并限制并发。"""

    def __init__(self, specs: dict[str, dict[str, Any]], assistant_id: str = "supervisor") -> None:
        """specs: {agent_name: AsyncSubAgent spec}；assistant_id = 主管自己的 assistant
        （回写唤醒 run 用；config 里若带 assistant_id 则优先用 config 的）。"""
        self._specs = specs
        self._assistant_id = assistant_id
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

    def _start_args(
        self, request: ToolCallRequest, refreshed: dict[str, dict[str, Any]]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """返回 (spec, error_msg)。refreshed 是刚核实过的任务状态（覆盖 state 里的）。"""
        args = request.tool_call.get("args") or {}
        agent_type = args.get("subagent_type", "")
        if err := self._validate(agent_type):
            return None, err
        tasks = {**((request.state or {}).get("async_tasks") or {}), **refreshed}
        if self._active_count({"async_tasks": tasks}) >= MAX_ACTIVE_TASKS:
            return None, self._over_limit(request.state)
        return self._specs[agent_type], None

    # ---------- 状态核实（防僵尸 running 卡死名额） ----------

    def _refreshed_sync(self, request: ToolCallRequest) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for task in ((request.state or {}).get("async_tasks") or {}).values():
            if task.get("status") in _TERMINAL:
                continue
            spec = self._specs.get(task.get("agent_name", ""))
            if spec is None or not task.get("run_id"):
                continue
            try:
                run = self._sync_client(spec).runs.get(task["thread_id"], task["run_id"])
            except Exception:  # noqa: BLE001 — 查不到就按原状态算，不阻断派活
                continue
            status = run.get("status")
            if status and status != task.get("status"):
                out[task["task_id"]] = {**task, "status": status, "last_checked_at": _now()}
        return out

    async def _refreshed_async(self, request: ToolCallRequest) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for task in ((request.state or {}).get("async_tasks") or {}).values():
            if task.get("status") in _TERMINAL:
                continue
            spec = self._specs.get(task.get("agent_name", ""))
            if spec is None or not task.get("run_id"):
                continue
            try:
                run = await self._async_client(spec).runs.get(task["thread_id"], task["run_id"])
            except Exception:  # noqa: BLE001
                continue
            status = run.get("status")
            if status and status != task.get("status"):
                out[task["task_id"]] = {**task, "status": status, "last_checked_at": _now()}
        return out

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

    def _config_for(self, request: ToolCallRequest, spec: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """(子 run 的 configurable, 错误文本)：本任务 space + 父上下文（回写唤醒用）。

        spec 绑定了 ``space`` 的子 agent 只发 ``[space]``（仅本任务 space），并先
        校验主管有该空间权限，没有则拒绝启动。父上下文只在有鉴权上下文
        （非本地 demo）时才转发；回写所需的 parent_user_identity 取服务端注入的
        langgraph_auth_user，客户端伪造不了。
        """
        cfg = request.runtime.config or {}
        conf = cfg.get("configurable") or {}
        space = spec.get("space")
        if space:
            spaces = allowed_spaces(cfg)
            if spaces is not None and space not in spaces:
                return None, (
                    f"用户没有空间 {space!r} 的权限，无法启动该子 agent"
                    f"（可用：{sorted(spaces)}）"
                )
            user_spaces: list[str] | None = [space]
        else:
            user_spaces = self._spaces_payload(cfg)
        payload: dict[str, Any] = {"user_spaces": user_spaces}

        parent_identity = current_identity(request.runtime.config)
        parent_thread_id = conf.get("thread_id")
        if parent_identity != "local" and parent_thread_id:
            parent_assistant = (
                conf.get("assistant_id")
                or (cfg.get("metadata") or {}).get("assistant_id")
                or self._assistant_id
            )
            payload.update(
                parent_thread_id=parent_thread_id,
                parent_assistant_id=parent_assistant,
                parent_user_identity=parent_identity,
                # 子 run 与父 run 同一部署，用 spec 的 url（本部署即 aegra）
                notify_url=spec.get("url"),
            )
        return {"configurable": payload}, None

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

    def _launched(
        self, request: ToolCallRequest, task: dict[str, Any], refreshed: dict[str, dict[str, Any]] | None = None
    ) -> Command[Any]:
        msg = f"Launched async subagent. task_id: {task['task_id']}"
        return Command(
            update={
                "messages": [ToolMessage(msg, tool_call_id=request.runtime.tool_call_id)],
                "async_tasks": {**(refreshed or {}), task["task_id"]: task},
            }
        )

    def _start_sync(self, request: ToolCallRequest) -> ToolMessage | Command[Any]:
        refreshed = self._refreshed_sync(request)
        spec, err = self._start_args(request, refreshed)
        if err:
            return self._err(request, err)
        assert spec is not None
        args = request.tool_call.get("args") or {}
        config, err = self._config_for(request, spec)
        if err:
            return self._err(request, err)
        client = self._sync_client(spec)
        try:
            thread = client.threads.create()
            run = client.runs.create(
                thread_id=thread["thread_id"],
                assistant_id=spec["graph_id"],
                input={"messages": [{"role": "user", "content": args.get("description", "")}]},
                # 前端代理要订阅子 agent 的实时输出，必须先声明这些流模式
                stream_mode=["messages", "custom"],
                config=config,
            )
        except Exception as e:  # noqa: BLE001 — SDK 异常无类型，回给模型
            logger.warning("failed_to_launch_async_subagent", error=str(e))
            return self._err(request, f"Failed to launch async subagent: {e}")
        return self._launched(
            request, self._task(thread["thread_id"], spec_name(spec, self._specs), run["run_id"]), refreshed
        )

    async def _astart(self, request: ToolCallRequest) -> ToolMessage | Command[Any]:
        refreshed = await self._refreshed_async(request)
        spec, err = self._start_args(request, refreshed)
        if err:
            return self._err(request, err)
        assert spec is not None
        args = request.tool_call.get("args") or {}
        config, err = self._config_for(request, spec)
        if err:
            return self._err(request, err)
        client = self._async_client(spec)
        try:
            thread = await client.threads.create()
            run = await client.runs.create(
                thread_id=thread["thread_id"],
                assistant_id=spec["graph_id"],
                input={"messages": [{"role": "user", "content": args.get("description", "")}]},
                # 前端代理要订阅子 agent 的实时输出，必须先声明这些流模式
                stream_mode=["messages", "custom"],
                config=config,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("failed_to_launch_async_subagent", error=str(e))
            return self._err(request, f"Failed to launch async subagent: {e}")
        return self._launched(
            request, self._task(thread["thread_id"], spec_name(spec, self._specs), run["run_id"]), refreshed
        )

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
        config, err = self._config_for(request, spec)
        if err:
            return self._err(request, err)
        client = self._sync_client(spec)
        try:
            run = client.runs.create(
                thread_id=tracked["thread_id"],
                assistant_id=spec["graph_id"],
                input={"messages": [{"role": "user", "content": (request.tool_call.get("args") or {}).get("message", "")}]},
                multitask_strategy="interrupt",
                # 前端代理要订阅子 agent 的实时输出，必须先声明这些流模式
                stream_mode=["messages", "custom"],
                config=config,
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
        config, err = self._config_for(request, spec)
        if err:
            return self._err(request, err)
        client = self._async_client(spec)
        try:
            run = await client.runs.create(
                thread_id=tracked["thread_id"],
                assistant_id=spec["graph_id"],
                input={"messages": [{"role": "user", "content": (request.tool_call.get("args") or {}).get("message", "")}]},
                multitask_strategy="interrupt",
                # 前端代理要订阅子 agent 的实时输出，必须先声明这些流模式
                stream_mode=["messages", "custom"],
                config=config,
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
