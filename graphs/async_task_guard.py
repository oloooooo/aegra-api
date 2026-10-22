"""拦截 async subagent 的派活与结果投递：spaces 收窄、子线程归属、结果写回主管 state。

===============================================================================
一、它拦哪两个工具
===============================================================================

deepagents 内置的 `start_async_task` / `update_async_task` 建子 run 时不带任何 config
（`AsyncSubAgent` spec 没有 config 字段，内置工具也没有关闭开关），所以我们短路
handler，自己用 spec 里的静态服务 token 建 thread/run，把两样东西塞进去：
权限（只给本任务的 space）与归属（哪个终端用户拥有这个子线程）。

check / cancel / list 不创建 run，直接放行给内置 handler；check 只额外记一笔
"这个结果已经给过主管了"（见 `_mark_checked`）。

===============================================================================
二、派活路径
===============================================================================

    start_async_task(subagent_type="cbam-reporting", description="…")
      │
      │ 1. _prepare()        subagent_type -> spec（名单来自 aegra.json 的图 + space）
      │ 2. _config_for()     what the child may read / who to notify
      │      configurable.user_spaces = ["cbam-reporting"]     ← 只给本任务的那个
      │      （spec 绑了 space 时还先校验主管有该 space，没有就拒绝）
      │      configurable.parent_thread_id / parent_assistant_id /
      │                   parent_user_identity / notify_url   ← 子 agent 收尾回写用
      │ 3. _thread_meta()   what the child thread is owned by
      │      metadata = {owner_identity: <终端用户>, parent_thread_id: <主管>}
      │      （子线程属主是服务账号，配额与级联删除靠这两个字段，见 ADR-0012）
      │ 4. POST /threads -> POST /threads/{tid}/runs
      │      stream_mode=["messages","custom"]（前端代理要订阅子 agent 实时输出）
      ▼
    Command(update={messages: [ToolMessage("Launched … task_id: …")],
                    async_tasks: {task_id: {...}}})

**没有并发上限**：一个主管能派多少个子 agent 不受限制。规模由 thread 配额
（ADR-0012）与 worker 队列（`N_JOBS_PER_WORKER`）承接，超额的子 run 在服务端排队。

===============================================================================
三、投递路径：把结果写进主管自己的 state
===============================================================================

子 agent 跑完，结果要进主管上下文。主管 run 中途没有任何 API 能往里塞消息，
所以只有两个时刻：run 开始（before_agent）与 run 结束（after_agent），各一次。

    before_agent / after_agent
      │ 1. _refreshed_async()  向子 run 问真实状态（state 里的 status 只是快照）
      │ 2. _plan()             谁已终态、且结果还没进过主管上下文
      │ 3. POST /custom/tasks/{id}/deliver-claim   抢投递权（与 notifier 互斥）
      │ 4. GET /threads/{child}                    取子 agent 最后一条消息当摘要
      │ 5. 返回 {"messages": [HumanMessage("[Async subagent … ] Result: …")],
      │          "async_tasks": {task_id: {..., delivered_run_id: run_id}}}
      ▼
    本次 run 的 checkpoint 里多一条 human 消息
    （前端 `toView` 靠 `[Async subagent` 前缀渲染成「子 agent 返回」卡片）

摘要截断到 500 字并带标记，**全文只有 check_async_task 能取**（那边不截断）。

判重两个来源：messages 里的投递消息（跨 run/跨进程都看得见）与 task 上的
`delivered_run_id`（模型自己 check 过也算已投，见 `_mark_checked`）。发件箱行
`(task_id, run_id)` 负责与分发者互斥：谁能把行从 pending 翻成 delivered，谁去写 state。

===============================================================================
四、多进程
===============================================================================

guard 自己不存任何跨请求状态（旧版的名额账本已随 ADR-0008 删除）。所有跨进程
判重都落在两处服务端：
  - `async_task_outbox(task_id, run_id)`：管“这次结果投没投过”（ADR-0010）；
  - `thread.metadata_json`：管“这个子线程是谁的”（ADR-0012）。

===============================================================================
五、代码分区（改代码前先看这张表）
===============================================================================

  A. 文本与判重    _TERMINAL, _now(), _shorten(), _message_text(),
                   _delivered_pairs(), _is_delivered(), _completion_text(),
                   _thread_summary()
  B. 派活          AsyncTaskGuard._key/_sync_client/_async_client, _prepare(),
                   _config_for(), _thread_meta(), _task(), _launched(),
                   _start_sync/_astart, _tracked(), _updated(),
                   _update_sync/_aupdate, _err()
  C. 投递          _spec_of(), _refresh_targets(), _refreshed_sync/_refreshed_async,
                   _summary_async(), _claim_async/_release_async, _plan(),
                   _deliver(), abefore_agent/aafter_agent, _mark_checked()
  D. 接线          wrap_tool_call / awrap_tool_call
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.config import get_config
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from langgraph_sdk import get_client, get_sync_client
from langgraph_sdk.client import LangGraphClient, SyncLangGraphClient

from custom.outbox import completion_text, message_text, shorten
from graphs.space_access import allowed_spaces, current_identity

logger = structlog.getLogger(__name__)

_TERMINAL = frozenset({"cancelled", "success", "error", "timeout", "interrupted"})

# 投递账本标记：格式定义在 custom/outbox.py（与分发者、finalize_run 共用同一口径）
_TASK_MARK = "task_id: "
_TASK_ID_RE = re.compile(r"task_id: ([0-9a-zA-Z_-]+)")
# 判重键含 run_id：update_async_task 会给同一任务开新 run，只认 task_id 会吞掉第二次结果
_RUN_ID_RE = re.compile(r"run_id: ([0-9a-zA-Z_-]+)")


# ============================ A. 文本与判重（纯函数） ============================


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shorten(text: str) -> str:
    """截断摘要——口径在 custom/outbox.py，三处必须逐字一致。"""
    return shorten(text)


def _message_text(msg: Any) -> str:
    """消息正文：兼容 str / content 块列表 / dict 三种形态。"""
    return message_text(msg)


def _delivered_pairs(messages: Any) -> set[tuple[str, str]]:
    """messages 里已投递过的 (task_id, run_id)。

    只认**投递消息**（`[Async subagent …` 前缀）：派活工具消息里也有 `task_id:`，
    拿它当判据会把"还没投递"误判成"已投递"，结果永远不投。
    """
    out: set[tuple[str, str]] = set()
    for msg in messages or []:
        text = _message_text(msg)
        if not text.startswith("[Async subagent"):
            continue
        task = _TASK_ID_RE.search(text)
        run = _RUN_ID_RE.search(text)
        if task and run:
            out.add((task.group(1), run.group(1)))
    return out


def _is_delivered(task: dict[str, Any], pairs: set[tuple[str, str]]) -> bool:
    """这个 run 的结果是否已经进过主管上下文。

    两个来源：投递消息（跨 run/跨进程都看得见），以及 `delivered_run_id`
    （模型自己 check 拿到终态结果时记下的，见 `_mark_checked`）。
    """
    run_id = task.get("run_id") or ""
    return task.get("delivered_run_id") == run_id or (task["task_id"], run_id) in pairs


def _completion_text(task: dict[str, Any], summary: str) -> str:
    """投递消息正文——格式口径在 custom/outbox.py。"""
    return completion_text(task["task_id"], task.get("run_id"), task.get("agent_name"), task.get("status"), summary)


def _thread_summary(thread: Any) -> str:
    """子 agent 的结论 = 它自己线程的最后一条消息（截断后）。"""
    messages = ((thread or {}).get("values") or {}).get("messages") or []
    return _shorten(_message_text(messages[-1])) if messages else "(no output)"


class AsyncTaskGuard(AgentMiddleware):
    """拦截 async subagent 的 run 创建：注入 user_spaces、给子线程打归属标记。"""

    def __init__(self, specs: dict[str, dict[str, Any]], assistant_id: str = "supervisor") -> None:
        """specs: {agent_name: AsyncSubAgent spec}；assistant_id = 主管自己的 assistant
        （回写唤醒 run 用；config 里若带 assistant_id 则优先用 config 的）。"""
        self._specs = specs
        self._assistant_id = assistant_id
        self._sync: dict[tuple, SyncLangGraphClient] = {}
        self._async: dict[tuple, LangGraphClient] = {}

    # =========================== B. 派活 ===========================

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

    # ---------- 前置：校验 + 权限 + 归属 ----------

    def _prepare(self, request: ToolCallRequest) -> tuple[str, dict[str, Any]] | str:
        """(agent 名, spec) 或错误文本。名字要写进 task，供之后的 update/投递反查。"""
        agent_type = (request.tool_call.get("args") or {}).get("subagent_type", "")
        spec = self._specs.get(agent_type)
        if spec is None:
            allowed = ", ".join(f"`{k}`" for k in self._specs)
            return f"Unknown async subagent type `{agent_type}`. Available types: {allowed}"
        return agent_type, spec

    def _config_for(self, request: ToolCallRequest, spec: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """(子 run 的 configurable, 错误文本)：本任务 space + 父上下文（收尾回写用）。

        spec 绑定了 ``space`` 的子 agent 只发 ``[space]``，并先校验主管有该空间权限；
        没有 space 绑定时回退主管的全量快照。父上下文只在有鉴权上下文（非本地 demo）
        时才转发，其中的 identity 取服务端注入的 `langgraph_auth_user`，客户端伪造不了。
        """
        cfg = request.runtime.config or {}
        conf = cfg.get("configurable") or {}
        space = spec.get("space")
        if space:
            spaces = allowed_spaces(cfg)
            if spaces is not None and space not in spaces:
                return None, f"用户没有空间 {space!r} 的权限，无法启动该子 agent（可用：{sorted(spaces)}）"
            user_spaces: list[str] | None = [space]
        else:
            all_spaces = allowed_spaces(cfg)
            user_spaces = None if all_spaces is None else sorted(all_spaces)
        payload: dict[str, Any] = {"user_spaces": user_spaces}

        parent_identity = current_identity(request.runtime.config)
        parent_thread_id = conf.get("thread_id")
        if parent_identity != "local" and parent_thread_id:
            payload.update(
                parent_thread_id=parent_thread_id,
                parent_assistant_id=(
                    conf.get("assistant_id") or (cfg.get("metadata") or {}).get("assistant_id") or self._assistant_id
                ),
                parent_user_identity=parent_identity,
                # 子 run 与父 run 同一部署，用 spec 的 url（本部署即 aegra）
                notify_url=spec.get("url"),
            )
        return {"configurable": payload}, None

    @staticmethod
    def _thread_meta(request: ToolCallRequest) -> dict[str, Any]:
        """子线程的归属标记：属主是服务账号，但配额与级联清理按终端用户算（ADR-0012）。"""
        conf = (request.runtime.config or {}).get("configurable") or {}
        identity = current_identity(request.runtime.config)
        return {
            "owner_identity": None if identity == "local" else identity,
            "parent_thread_id": conf.get("thread_id"),
        }

    @staticmethod
    def _task(task_id: str, agent_name: str, run_id: str) -> dict[str, Any]:
        """新任务记录（thread_id == task_id：子线程就是任务句柄）。"""
        now = _now()
        return {
            "task_id": task_id,
            "agent_name": agent_name,
            "thread_id": task_id,
            "run_id": run_id,
            "status": "running",
            "created_at": now,
            "last_checked_at": now,
            "last_updated_at": now,
        }

    @staticmethod
    def _launched(
        request: ToolCallRequest, task: dict[str, Any], refreshed: dict[str, dict[str, Any]]
    ) -> Command[Any]:
        """派活成功的 state 更新：工具消息 + 新任务 + 顺手刷新的旧任务状态。"""
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        f"Launched async subagent. task_id: {task['task_id']}",
                        tool_call_id=request.runtime.tool_call_id,
                    )
                ],
                "async_tasks": {**refreshed, task["task_id"]: task},
            }
        )

    def _err(self, request: ToolCallRequest, msg: str) -> ToolMessage:
        """普通错误回给模型（不走 HITL：它自己等一等或换个问法即可）。"""
        return ToolMessage(content=msg, tool_call_id=request.runtime.tool_call_id, name=request.tool_call.get("name"))

    # ---------- start（sync / async 只差 client 与 await） ----------

    def _start_args(
        self, request: ToolCallRequest, spec: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], str] | str:
        """(子 run config, 子线程 metadata, description) 或错误文本。"""
        config, err = self._config_for(request, spec)
        if err:
            return err
        assert config is not None
        return config, self._thread_meta(request), (request.tool_call.get("args") or {}).get("description", "")

    def _start_sync(self, request: ToolCallRequest) -> ToolMessage | Command[Any]:
        refreshed = self._refreshed_sync((request.state or {}).get("async_tasks") or {})
        prepared = self._prepare(request)
        if isinstance(prepared, str):
            return self._err(request, prepared)
        name, spec = prepared
        args = self._start_args(request, spec)
        if isinstance(args, str):
            return self._err(request, args)
        config, meta, description = args
        try:
            thread = self._sync_client(spec).threads.create(metadata=meta)
            run = self._sync_client(spec).runs.create(
                thread_id=thread["thread_id"],
                assistant_id=spec["graph_id"],
                input={"messages": [{"role": "user", "content": description}]},
                stream_mode=["messages", "custom"],
                config=config,
            )
        except Exception as e:  # noqa: BLE001 — SDK 异常无类型，回给模型
            logger.warning("failed_to_launch_async_subagent", error=str(e))
            return self._err(request, f"Failed to launch async subagent: {e}")
        return self._launched(request, self._task(thread["thread_id"], name, run["run_id"]), refreshed)

    async def _astart(self, request: ToolCallRequest) -> ToolMessage | Command[Any]:
        refreshed = await self._refreshed_async((request.state or {}).get("async_tasks") or {})
        prepared = self._prepare(request)
        if isinstance(prepared, str):
            return self._err(request, prepared)
        name, spec = prepared
        args = self._start_args(request, spec)
        if isinstance(args, str):
            return self._err(request, args)
        config, meta, description = args
        client = self._async_client(spec)
        try:
            thread = await client.threads.create(metadata=meta)
            run = await client.runs.create(
                thread_id=thread["thread_id"],
                assistant_id=spec["graph_id"],
                input={"messages": [{"role": "user", "content": description}]},
                stream_mode=["messages", "custom"],
                config=config,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("failed_to_launch_async_subagent", error=str(e))
            return self._err(request, f"Failed to launch async subagent: {e}")
        return self._launched(request, self._task(thread["thread_id"], name, run["run_id"]), refreshed)

    # ---------- update（新 run 同样要带 config，否则子 agent 中途丢 spaces） ----------

    @staticmethod
    def _tracked(request: ToolCallRequest) -> tuple[dict[str, Any], str] | str:
        """(被更新的任务, 追加给子 agent 的消息) 或错误文本。"""
        args = request.tool_call.get("args") or {}
        task_id = str(args.get("task_id") or "").strip()
        tracked = ((request.state or {}).get("async_tasks") or {}).get(task_id)
        if not tracked:
            return f"No tracked task found for task_id: {task_id!r}"
        return tracked, args.get("message", "")

    def _update_ready(
        self, request: ToolCallRequest
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str] | str:
        """(任务, spec, 子 run config, 追加消息) 或错误文本——update 的前置合并成一处。"""
        tracked = self._tracked(request)
        if isinstance(tracked, str):
            return tracked
        task, message = tracked
        spec = self._specs.get(task["agent_name"])
        if spec is None:
            return f"Unknown agent for task: {task['task_id']!r}"
        config, err = self._config_for(request, spec)
        if err:
            return err
        assert config is not None
        return task, spec, config, message

    @staticmethod
    def _updated(request: ToolCallRequest, task: dict[str, Any], run_id: str) -> Command[Any]:
        """update 成功的 state 更新：任务换新 run、回到 running。"""
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        f"Updated async subagent. task_id: {task['task_id']}",
                        tool_call_id=request.runtime.tool_call_id,
                    )
                ],
                "async_tasks": {
                    task["task_id"]: {**task, "run_id": run_id, "status": "running", "last_updated_at": _now()}
                },
            }
        )

    def _update_sync(self, request: ToolCallRequest) -> ToolMessage | Command[Any]:
        ready = self._update_ready(request)
        if isinstance(ready, str):
            return self._err(request, ready)
        task, spec, config, message = ready
        try:
            run = self._sync_client(spec).runs.create(
                thread_id=task["thread_id"],
                assistant_id=spec["graph_id"],
                input={"messages": [{"role": "user", "content": message}]},
                multitask_strategy="interrupt",
                stream_mode=["messages", "custom"],
                config=config,
            )
        except Exception as e:  # noqa: BLE001
            return self._err(request, f"Failed to update async subagent: {e}")
        return self._updated(request, task, run["run_id"])

    async def _aupdate(self, request: ToolCallRequest) -> ToolMessage | Command[Any]:
        ready = self._update_ready(request)
        if isinstance(ready, str):
            return self._err(request, ready)
        task, spec, config, message = ready
        try:
            run = await self._async_client(spec).runs.create(
                thread_id=task["thread_id"],
                assistant_id=spec["graph_id"],
                input={"messages": [{"role": "user", "content": message}]},
                multitask_strategy="interrupt",
                stream_mode=["messages", "custom"],
                config=config,
            )
        except Exception as e:  # noqa: BLE001
            return self._err(request, f"Failed to update async subagent: {e}")
        return self._updated(request, task, run["run_id"])

    # =========================== C. 投递 ===========================

    def _spec_of(self, task: dict[str, Any]) -> dict[str, Any] | None:
        """任务对应的 spec：优先按 agent_name 找；该 space 已被删时退到任意一个同部署 spec。

        已派出的任务保留当时授权的 space（删 space 不影响旧任务投递），而这里的 spec
        只用来拼 url/headers 去查子 run、取摘要——它们跟 space 无关。

        shortcut: 所有子 agent 跑在同一部署（url/headers 相同），所以借一个就行；
        将来多部署时要把 url 记进 task。
        """
        if not task.get("run_id"):
            return None
        spec = self._specs.get(task.get("agent_name", ""))
        if spec is not None:
            return spec
        return next(iter(self._specs.values()), None)

    @staticmethod
    def _refresh_targets(tasks: dict[str, Any]) -> list[dict[str, Any]]:
        """需要向服务端核实的任务：还非终态、且有 run_id（sync/async 两条路共用）。"""
        return [t for t in tasks.values() if t.get("status") not in _TERMINAL and t.get("run_id")]

    def _refreshed_sync(self, tasks: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """查到的真实状态变化（只含变了的）。查不到就按原状态算，不阻断派活。"""
        out: dict[str, dict[str, Any]] = {}
        for task in self._refresh_targets(tasks):
            spec = self._spec_of(task)
            if spec is None:
                continue
            try:
                run = self._sync_client(spec).runs.get(task["thread_id"], task["run_id"])
            except Exception:  # noqa: BLE001
                continue
            status = run.get("status")
            if status and status != task.get("status"):
                out[task["task_id"]] = {**task, "status": status, "last_checked_at": _now()}
        return out

    async def _refreshed_async(self, tasks: dict[str, Any]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for task in self._refresh_targets(tasks):
            spec = self._spec_of(task)
            if spec is None:
                continue
            try:
                run = await self._async_client(spec).runs.get(task["thread_id"], task["run_id"])
            except Exception:  # noqa: BLE001
                continue
            status = run.get("status")
            if status and status != task.get("status"):
                out[task["task_id"]] = {**task, "status": status, "last_checked_at": _now()}
        return out

    async def _summary_async(self, task: dict[str, Any]) -> str:
        """取子 agent 的最终结论（摘要）。拿不到就给占位，不阻断投递。

        注：发件箱行里也存了一份写入时的摘要快照（分发者用的）；这里现查线程是为了
        拿到**当下**的最后一条消息（同一条 run 里模型可能后来又追加了内容）。
        快照与现查是同一套截断口径（custom/outbox.py），不会出现两种格式。
        """
        spec = self._spec_of(task)
        if spec is None or not task.get("thread_id"):
            return "(no output)"
        try:
            return _thread_summary(await self._async_client(spec).threads.get(task["thread_id"]))
        except Exception:  # noqa: BLE001
            return "(summary unavailable)"

    @staticmethod
    def _claim_endpoint(
        spec: dict[str, Any] | None, task: dict[str, Any], parent_thread_id: str
    ) -> tuple[str, dict[str, Any], dict[str, str]] | None:
        """认领端点 + body + headers；缺信息时返回 None（不认领、但也不阻断投递）。

        认领 = 把发件箱行从 pending 原子翻成 delivered（见 custom/delivery_api.py）。
        没有时间窗口、没有“让路”参数：一行一判，同批多个任务互不影响。
        """
        if not parent_thread_id or spec is None or not spec.get("url"):
            return None
        endpoint = f"{str(spec['url']).rstrip('/')}/custom/tasks/{task['task_id']}/deliver-claim"
        return endpoint, {
            "parent_thread_id": parent_thread_id,
            "run_id": task.get("run_id"),
        }, dict(spec.get("headers") or {})

    async def _claim_async(self, task: dict[str, Any], parent_thread_id: str) -> bool | None:
        """抢投递权：True 抢到 / False 别人投过 / None 无法判定（照常投，宁可多一条）。"""
        req = self._claim_endpoint(self._spec_of(task), task, parent_thread_id)
        if req is None:
            return None
        endpoint, body, headers = req
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(endpoint, json=body, headers=headers)
                resp.raise_for_status()
                return bool(resp.json().get("claimed"))
        except Exception:  # noqa: BLE001
            logger.warning("task_delivery_claim_failed", task_id=task.get("task_id"))
            return None

    async def _release_async(self, task: dict[str, Any], parent_thread_id: str) -> None:
        """撤销认领：state 没写成时调，否则这次结果会被判为「已投递」而丢掉。"""
        req = self._claim_endpoint(self._spec_of(task), task, parent_thread_id)
        if req is None:
            return
        endpoint, body, headers = req
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.delete(endpoint, params=body, headers=headers)
        except Exception:  # noqa: BLE001
            logger.warning("task_delivery_release_failed", task_id=task.get("task_id"))

    @staticmethod
    def _plan(
        state: Any, fresh: dict[str, str | None]
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """(要写回 state 的状态更新, 待投递任务)。fresh 只含本轮查到的非终态任务新状态。"""
        tasks = (state or {}).get("async_tasks") or {}
        delivered = _delivered_pairs((state or {}).get("messages") or [])
        changed: dict[str, dict[str, Any]] = {}
        pending: list[dict[str, Any]] = []
        for task_id, task in tasks.items():
            status = task.get("status")
            if status not in _TERMINAL:
                new = fresh.get(task_id)
                if new and new != status:
                    status = new
                    changed[task_id] = {**task, "status": status, "last_checked_at": _now()}
            # 工具路径可能已把 state 写成终态却没投递（_refreshed_* 只改状态），这里补投
            if status in _TERMINAL and not _is_delivered(task, delivered):
                pending.append({**task, "status": status})
        return changed, pending

    async def _deliver(self, state: Any) -> dict[str, Any] | None:
        """把"已完成且未投递"的子任务结果写进本次 run 的 state。

        只有两个调用点（`before_agent` / `after_agent`，一次 run 各一次）：前者补上一个
        run 结束后才完成的任务，后者落定这次跑动期间完成的任务（模型已没机会转述）。
        认领后若写 state 抛异常，会把认领撤掉再抛，避免结果被永久锁成「已投递」。
        """
        tasks = (state or {}).get("async_tasks") or {}
        fresh = {tid: t["status"] for tid, t in (await self._refreshed_async(tasks)).items()}
        changed, pending = self._plan(state, fresh)
        parent = self._parent_thread_id()
        texts: list[str] = []
        claimed: list[dict[str, Any]] = []
        try:
            for task in pending:
                if await self._claim_async(task, parent) is False:  # notifier 已投过
                    logger.info("task_delivery_skipped_claim", task_id=task.get("task_id"))
                    continue
                claimed.append(task)
                texts.append(_completion_text(task, await self._summary_async(task)))
                # 记在 task 上：同路判重不再靠扫消息，check 过的也不会被再投一次
                changed[task["task_id"]] = {
                    **changed.get(task["task_id"], task),
                    "status": task["status"],
                    "delivered_run_id": task.get("run_id"),
                }
        except Exception:
            for task in claimed:
                await self._release_async(task, parent)
            raise
        if not changed and not texts:
            return None
        out: dict[str, Any] = {}
        if changed:
            out["async_tasks"] = changed
        if texts:
            out["messages"] = [HumanMessage(content=t) for t in texts]
            # 投递成功才留痕：查日志时能分清"这轮谁投的"与"分发者另开的"
            logger.info("task_results_delivered", tasks=[t["task_id"] for t in claimed], count=len(texts))
        return out

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """run 开始：补上父线程空闲期间完成、通知路没投出去的结果。"""
        return await self._deliver(state)

    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """run 结束：把这次跑动期间完成的子任务结果落进 state。"""
        return await self._deliver(state)

    @staticmethod
    def _parent_thread_id() -> str:
        """当前（主管）run 的 thread_id；hook 在图外被触发时返回空串。"""
        try:
            conf = (get_config() or {}).get("configurable") or {}
        except RuntimeError:
            return ""
        return str(conf.get("thread_id") or "")

    def _mark_checked(self, request: ToolCallRequest, result: Any) -> Any:
        """模型自己 check 到终态结果时补记 delivered_run_id，否则下一个 hook 会再投一遍。

        check 返回的是 ToolMessage(json)，扫消息的判重认不出它；这里在 state 的 task 上
        留记号，`_plan` 据此跳过（精确到 run_id，update_async_task 换 run 后照常再投）。
        """
        task_id = str((request.tool_call.get("args") or {}).get("task_id") or "").strip()
        task = ((request.state or {}).get("async_tasks") or {}).get(task_id)
        update = getattr(result, "update", None)
        if task is None or not isinstance(update, dict):
            return result
        checked = (update.get("async_tasks") or {}).get(task_id) or {}
        if checked.get("status") in _TERMINAL:
            update["async_tasks"] = {
                **(update.get("async_tasks") or {}),
                task_id: {**checked, "delivered_run_id": task.get("run_id")},
            }
        return result

    # =========================== D. 接线 ===========================

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[..., Any]) -> ToolMessage | Command[Any]:
        """同步接线：start/update 自己建 run，check 之后补记一次投递账。"""
        name = request.tool_call.get("name")
        if name == "start_async_task":
            return self._start_sync(request)
        if name == "update_async_task":
            return self._update_sync(request)
        result = handler(request)
        return self._mark_checked(request, result) if name == "check_async_task" else result

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[..., Awaitable[Any]]
    ) -> ToolMessage | Command[Any]:
        """异步接线（线上走这条）：同上。"""
        name = request.tool_call.get("name")
        if name == "start_async_task":
            return await self._astart(request)
        if name == "update_async_task":
            return await self._aupdate(request)
        result = await handler(request)
        return self._mark_checked(request, result) if name == "check_async_task" else result
