"""Completion notifier middleware for async subagents.

子 agent 跑完（成功或出错）时，主动往父线程（主管）推一条消息，让它醒来把结果
转达给用户，而不必等用户再问 `check_async_task`。

**这是 bring-your-own 组件**：async subagent 协议本身没有完成通知机制，默认只有
主管主动 `check_async_subagent` 才拿得到结果。本中间件补上这个缺口。

## 回写为什么不能用服务 token（ADR-0005 选项1）

aegra 的 `runs.create` 只允许线程属主写（runs.py 查 `thread.user_id != user.identity`
→ 404）。服务身份拿不到父线程，所以回写必须以**父用户自己的身份**写。

做法：拦截层（graphs/async_task_guard.py）把父上下文经子 run 的 ``configurable``
转发进来——``parent_thread_id`` / ``parent_assistant_id`` / ``parent_user_identity``
（取服务端注入的 ``langgraph_auth_user``，客户端伪造不了）/ ``notify_url``。本中间件在
hook 内用 ``get_config()`` 读到它们后：

1. 用 env 的 ``JWT_SECRET`` **现铸一枚短时父身份 token**（不写进 config/DB，用完即弃）；
2. 用显式 ``get_client(url=notify_url, headers=Bearer <父token>)`` 回写父线程。

于是唤醒 run 以父用户身份跑（spaces 正常），同用户写自己的线程，归属校验通过。

**不转发父上下文 = 不回写**（本地直跑 demo、或没走拦截层的部署），只是安静跳过。

## 什么时候投递、怎么保证只投一次

按父线程状态三态分派：`busy` → 不插队（主管会在下一个模型调用前通过
`AsyncTaskGuard.abefore_model` 注入结果），只安排延迟复查；`interrupted` → 一律不动
（否则会把用户正等着回答的答案顶掉）；`idle` / `error` → 开一轮新 run 投递。

判重交给服务端的认领表（`custom/delivery_api.py`，主键 `(task_id, run_id)`）：
抢到认领才投，创建 run 失败撤回答领，同线程 8 秒内已有认领则让路。
所以本中间件既不需要读 thread state 判重，也不依赖进程内状态——多进程部署同样成立。

## 兼容官方 factory 模式

官方例子（graphs/official_researcher.py）用 graph factory 把父 id 从 config 传进
构造函数。本组件同时支持两条路：优先从 ``get_config()`` 读（拦截层注入的那套），
读不到再退回构造函数传入的值。但要铸 token 还缺 ``parent_user_identity`` 与
``notify_url``，两者只能来自 config 或显式构造参数——缺任何一个都跳过并告警，
绝不静默失败（这正是旧版「试了不太行」的病灶之一）。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from typing import Any

import httpx
import jwt
import structlog
from langchain.agents.middleware.types import AgentMiddleware
from langgraph.config import get_config
from langgraph.runtime import Runtime
from langgraph_sdk import get_client

logger = structlog.getLogger(__name__)

# 短时父 token 有效期：只够完成一次回写，越短泄露面越小（ADR-0005）
_PARENT_TOKEN_TTL = 300

# 去重集合的上限：只增不减会随进程寿命缓慢涨；淘汰最旧，最坏多一次重复通知
_NOTIFIED_MAX = 4096


# 允许 resume 的中断白名单。**现在为空**：① 方案下父 run 不会为等任务而挂起，
# 所以 notifier 一个 interrupted 线程都不碰（HITL 提问、未知中断都交给 ① 兜底）。
# 将来做"协作式挂起"时，把那种中断的 kind 加进来即可。
_RESUMABLE_KINDS: frozenset[str] = frozenset()

# 父线程正忙：不插队，等父 run 结束后再确认一次"① 是否已经投递过"
_RECHECK_DELAYS = (8, 20, 45)

# 延迟复查的 task 需要留引用，否则可能被 GC（asyncio 只持弱引用）
_RECHECK_TASKS: set[asyncio.Task[Any]] = set()


def _interrupt_kinds(snapshot: dict[str, Any]) -> set[str]:
    """待恢复中断的判别字段（我们自己的 ask 用 value.tool 标识）。"""
    kinds: set[str] = set()
    for item in (snapshot or {}).get("interrupts") or []:
        value = item.get("value") if isinstance(item, dict) else getattr(item, "value", None)
        if isinstance(value, dict):
            kinds.add(str(value.get("kind") or value.get("tool") or "unknown"))
        else:
            kinds.add("unknown")
    return kinds


async def _claim_delivery(url: str, token: str, parent_thread_id: str, task_id: str, run_id: str) -> tuple[bool, str]:
    """向服务端认领这次执行结果的投递权（跨进程幂等；见 custom/delivery_api.py）。"""
    endpoint = f"{url.rstrip('/')}/custom/tasks/{task_id}/deliver-claim"
    body = {"parent_thread_id": parent_thread_id, "run_id": run_id}
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(endpoint, json=body, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
    return bool(payload.get("claimed")), str(payload.get("reason") or "")


async def _release_delivery(url: str, token: str, parent_thread_id: str, task_id: str, run_id: str) -> None:
    """撤销认领：投递 run 没创建成功时调用，否则这次结果会被永久判为"已投递"。"""
    endpoint = f"{url.rstrip('/')}/custom/tasks/{task_id}/deliver-claim"
    params = {"parent_thread_id": parent_thread_id, "run_id": run_id}
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=10) as client:
        await client.delete(endpoint, params=params, headers=headers)


def _mint_parent_token(identity: str) -> str:
    """现铸一枚短时父身份 token（sub/iat/exp，HS256——custom_auth 要求的三件套）。"""
    now = int(time.time())
    return jwt.encode(
        {"sub": identity, "iat": now, "exp": now + _PARENT_TOKEN_TTL},
        os.environ["JWT_SECRET"],
        algorithm="HS256",
    )


class CompletionNotifierMiddleware(AgentMiddleware):
    """子 agent 完成/出错时，往父线程推一条唤醒消息。

    Args:
        parent_thread_id: 父线程 ID（通常从 config 读，这里是回退值）。
        parent_assistant_id: 父 assistant ID（创建 run 需要）。
        parent_user_identity: 父用户 identity——铸回写 token 必需。
        notify_url: aegra base URL——显式回写客户端必需。
        subagent_name: 日志/消息里用的可读名。
    """

    def __init__(
        self,
        parent_thread_id: str | None = None,
        parent_assistant_id: str | None = None,
        parent_user_identity: str | None = None,
        notify_url: str | None = None,
        subagent_name: str | None = None,
    ):
        self.parent_thread_id = parent_thread_id
        self.parent_assistant_id = parent_assistant_id
        self.parent_user_identity = parent_user_identity
        self.notify_url = notify_url
        self.subagent_name = subagent_name or "subagent"
        # 按 run_id 去重：静态图的 middleware 实例在同 worker 内被并发 run 共享，
        # 用 set 而非 bool 位，避免一个 run 的完成把另一个 run 的通知吞掉。
        self._notified_runs: set[str] = set()
        # 插入顺序，配合 set 做有界淘汰
        self._notified_order: deque[str] = deque()

    # ---------- 解析父上下文（config 优先，构造值回退） ----------

    @staticmethod
    def _config() -> dict[str, Any]:
        try:
            return get_config() or {}
        except RuntimeError:  # hook 在图外被触发（测试/脚本）
            return {}

    def _resolve_parent(self) -> tuple[str, str, str, str] | None:
        """返回 (thread_id, assistant_id, identity, url)；缺任何一项都放弃回写。"""
        conf = self._config().get("configurable") or {}
        thread_id = conf.get("parent_thread_id") or self.parent_thread_id
        assistant_id = conf.get("parent_assistant_id") or self.parent_assistant_id
        identity = conf.get("parent_user_identity") or self.parent_user_identity
        url = conf.get("notify_url") or self.notify_url
        if not (thread_id and assistant_id and identity and url):
            logger.warning(
                "completion_notify_skipped_incomplete_parent_context",
                parent_thread_id=bool(thread_id),
                parent_assistant_id=bool(assistant_id),
                parent_user_identity=bool(identity),
                notify_url=bool(url),
                subagent=self.subagent_name,
            )
            return None
        return str(thread_id), str(assistant_id), str(identity), str(url)

    def _run_key(self) -> str:
        conf = self._config().get("configurable") or {}
        return str(conf.get("run_id") or conf.get("thread_id") or "unknown-run")

    # ---------- 回写（四态分派） ----------

    async def _send_notification(self, message: str, task_id: str | None = None) -> None:
        """按父线程此刻的状态决定怎么投递（不再是"一律开一个 run"）。

        - busy：父 run 还在跑 → 不插队，①（before_model 注入）会在它的下一个边界
          把结果送进去；这里只安排延迟复查，兜住"父 run 正好在收尾"的缺口。
        - interrupted：图在等某个 resume 值（HITL 提问等）→ 一律不动，否则会把
          用户要回答的答案顶掉；用户答完后 ① 会在下一轮补上。
        - idle / error：线程空着 → 用 input 开新一轮投递（已经是消息就跳过）。
        """
        parent = self._resolve_parent()
        if parent is None:
            return
        run_key = self._run_key()
        if not self._mark_once(run_key):
            return
        thread_id, assistant_id, identity, url = parent
        task_id = task_id or str((self._config().get("configurable") or {}).get("thread_id") or thread_id)
        # 末尾带 task_id：主管侧与这里的判重都靠这个标记（两边投递只留一份）
        message = f"{message}\n\ntask_id: {task_id}\nrun_id: {run_key}"
        ctx = (thread_id, assistant_id, identity, url, message, task_id, run_key)
        await self._deliver(ctx)

    async def _deliver(self, ctx: tuple[str, str, str, str, str, str, str], attempt: int = 1, recheck: int = 0) -> None:
        thread_id, assistant_id, identity, url, message, task_id, run_key = ctx
        try:
            token = _mint_parent_token(identity)
            client = get_client(url=url, headers={"Authorization": f"Bearer {token}"})
            status = await self._thread_status(client, thread_id)

            if status == "busy":
                self._defer(ctx, recheck)
                return
            if status == "unknown":
                raise RuntimeError("parent thread unreachable")

            if status == "interrupted":
                # 只在这一种情况下需要 state：要知道挂的是哪种中断（HITL 提问 vs 将来我们自己造的）
                snapshot = await self._thread_state(client, thread_id)
                kinds = _interrupt_kinds(snapshot)
                if not (kinds & _RESUMABLE_KINDS):
                    logger.info(
                        "completion_notify_skipped_interrupted",
                        parent_thread_id=thread_id,
                        subagent=self.subagent_name,
                        interrupt_kinds=sorted(kinds) or ["none"],
                    )
                    return
                claimed, reason = await _claim_delivery(url, token, thread_id, task_id, run_key)
                if not claimed:
                    logger.info(
                        "completion_notify_skipped_claim",
                        parent_thread_id=thread_id,
                        subagent=self.subagent_name,
                        reason=reason,
                    )
                    return
                # 白名单命中（目前为空）：将来做协作式挂起时走这里
                await client.runs.create(
                    thread_id=thread_id, assistant_id=assistant_id, command={"resume": message}
                )
                logger.info("completion_notify_resumed", parent_thread_id=thread_id, subagent=self.subagent_name)
                return

            # 跨进程认领：主键 (task_id, run_id)；同一线程刚有人认领时让路（in_flight），
            # 这样不会出现两个投递 run 并发写同一线程。
            claimed, reason = await _claim_delivery(url, token, thread_id, task_id, run_key)
            if not claimed:
                logger.info(
                    "completion_notify_skipped_claim",
                    parent_thread_id=thread_id,
                    subagent=self.subagent_name,
                    task_id=task_id,
                    reason=reason,
                )
                return
            try:
                await client.runs.create(
                    thread_id=thread_id,
                    assistant_id=assistant_id,
                    input={"messages": [{"role": "user", "content": message}]},
                )
            except Exception:
                # 没投出去就把认领撤掉，否则这次结果会被永久判为"已投递"
                await _release_delivery(url, token, thread_id, task_id, run_key)
                raise
            logger.info(
                "completion_notify_sent",
                parent_thread_id=thread_id,
                subagent=self.subagent_name,
                task_id=task_id,
                attempt=attempt,
            )
        except Exception as e:  # noqa: BLE001 —— 通知是尽力而为，失败要留痕不能吞
            if attempt >= 3:
                self._notified_runs.discard(run_key)  # 释放占位，后续 hook 还能再试
                logger.warning(
                    "completion_notify_failed",
                    parent_thread_id=thread_id,
                    subagent=self.subagent_name,
                    error=str(e),
                    attempt=attempt,
                    exc_info=True,
                )
                return
            logger.info(
                "completion_notify_retry",
                parent_thread_id=thread_id,
                subagent=self.subagent_name,
                attempt=attempt,
                error=str(e),
            )
            await asyncio.sleep(0.5 * attempt)
            await self._deliver(ctx, attempt + 1, recheck)

    def _defer(self, ctx: tuple[str, str, str, str, str, str, str], recheck: int) -> None:
        """父线程忙：过一会儿再确认。1 ① 已经注入 → 跳过；2 父 run 收尾没注入 → 补投。"""
        if recheck >= len(_RECHECK_DELAYS):
            logger.info(
                "completion_notify_gave_up_busy",
                parent_thread_id=ctx[0],
                subagent=self.subagent_name,
            )
            return
        delay = _RECHECK_DELAYS[recheck]
        task = asyncio.create_task(self._recheck(ctx, recheck + 1, delay))
        _RECHECK_TASKS.add(task)
        task.add_done_callback(_RECHECK_TASKS.discard)

    async def _recheck(self, ctx: tuple[str, str, str, str, str, str, str], recheck: int, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._deliver(ctx, 1, recheck)

    @staticmethod
    async def _thread_status(client: Any, thread_id: str) -> str:
        """父线程状态；查不到返回 "unknown"。busy 时不必再花一次请求取 state。"""
        try:
            thread = await client.threads.get(thread_id)
        except Exception:  # noqa: BLE001
            return "unknown"
        return str((thread or {}).get("status") or "idle")

    @staticmethod
    async def _thread_state(client: Any, thread_id: str) -> dict[str, Any]:
        try:
            return await client.threads.get_state(thread_id) or {}
        except Exception:  # noqa: BLE001 —— 拿不到快照就当没有投递记录
            return {}

    def _mark_once(self, run_key: str) -> bool:
        """首次见到这个 run_key 返回 True；重复返回 False。

        去重集合保量：超过上限时按插入顺序淘汰最旧（最坏情况多一次重复通知）。
        """
        if run_key in self._notified_runs:
            return False
        self._notified_runs.add(run_key)
        self._notified_order.append(run_key)
        while len(self._notified_order) > _NOTIFIED_MAX:
            self._notified_runs.discard(self._notified_order.popleft())
        return True

    # ---------- hooks ----------

    @staticmethod
    def _extract_last_message(state: dict[str, Any]) -> str:
        messages = state.get("messages", [])
        if not messages:
            return "(no output)"
        last = messages[-1]
        content = getattr(last, "content", None)
        if content is None and isinstance(last, dict):
            content = last.get("content", "")
        content = content if content is not None else str(last)
        return content[:500] if isinstance(content, str) else str(content)[:500]

    def _task_id(self) -> str | None:
        """子 run 的 thread_id 就是父侧记的 task_id（guard 建任务时两者相同）。"""
        return str((self._config().get("configurable") or {}).get("thread_id") or "") or None

    async def aafter_agent(self, state: dict[str, Any], runtime: Runtime) -> dict[str, Any] | None:
        """成功完成时：把结果摘要交给父线程（忙/挂起不插队，详见 _send_notification）。"""
        summary = self._extract_last_message(state)
        await self._send_notification(
            f"[Async subagent '{self.subagent_name}' has completed] Result: {summary}", self._task_id()
        )
        return None

    async def awrap_model_call(self, request, handler):
        """模型调用出错时：推错误通知，再把异常抛回去。"""
        try:
            return await handler(request)
        except Exception as e:  # noqa: BLE001
            await self._send_notification(
                f"[Async subagent '{self.subagent_name}' encountered an error] Error: {e!s}",
                self._task_id(),
            )
            raise


def build_completion_notifier(
    parent_thread_id: str | None = None,
    parent_assistant_id: str | None = None,
    parent_user_identity: str | None = None,
    notify_url: str | None = None,
    subagent_name: str | None = None,
) -> CompletionNotifierMiddleware:
    """构造完成通知中间件（参数皆可选：拦截层部署下从 config 读，无需传）。"""
    return CompletionNotifierMiddleware(
        parent_thread_id=parent_thread_id,
        parent_assistant_id=parent_assistant_id,
        parent_user_identity=parent_user_identity,
        notify_url=notify_url,
        subagent_name=subagent_name,
    )
