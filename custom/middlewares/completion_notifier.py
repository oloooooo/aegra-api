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


class _ParentBusy(Exception):
    """父线程正忙：让回写走退避重试，而不是与用户 run 并发写同一线程。"""


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

    # ---------- 回写 ----------

    async def _send_notification(self, message: str) -> None:
        parent = self._resolve_parent()
        if parent is None:
            return
        run_key = self._run_key()
        if not self._mark_once(run_key):
            return

        thread_id, assistant_id, identity, url = parent
        for attempt in (1, 2, 3):
            try:
                token = _mint_parent_token(identity)
                client = get_client(url=url, headers={"Authorization": f"Bearer {token}"})
                # 父线程正在跑另一个 run 时不硬撞（aegra 不拦并发写），等它空闲再唤醒
                if await self._parent_busy(client, thread_id):
                    raise _ParentBusy(thread_id)
                await client.runs.create(
                    thread_id=thread_id,
                    assistant_id=assistant_id,
                    input={"messages": [{"role": "user", "content": message}]},
                )
                logger.info(
                    "completion_notify_sent",
                    parent_thread_id=thread_id,
                    subagent=self.subagent_name,
                    run_key=run_key,
                    attempt=attempt,
                )
                return
            except Exception as e:  # noqa: BLE001 —— 通知是尽力而为，失败要留痕不能吞
                if attempt == 3:
                    # 放弃时释放占位，让后续 hook（同一 run 的其它阶段）还能重试
                    self._notified_runs.discard(run_key)
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

    @staticmethod
    async def _parent_busy(client: Any, thread_id: str) -> bool:
        """父线程是否正在跑别的 run（aegra 对非 interrupt 策略不拦并发写）。"""
        try:
            state = await client.threads.get(thread_id)
        except Exception:  # noqa: BLE001 —— 查不到就不阻塞通知
            return False
        return str(state.get("status") or "") == "busy"

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

    async def aafter_agent(self, state: dict[str, Any], runtime: Runtime) -> dict[str, Any] | None:
        """成功完成时：推结果摘要给父线程。"""
        summary = self._extract_last_message(state)
        await self._send_notification(
            f"[Async subagent '{self.subagent_name}' has completed] Result: {summary}"
        )
        return None

    async def awrap_model_call(self, request, handler):
        """模型调用出错时：推错误通知，再把异常抛回去。"""
        try:
            return await handler(request)
        except Exception as e:  # noqa: BLE001
            await self._send_notification(
                f"[Async subagent '{self.subagent_name}' encountered an error] Error: {e!s}"
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
