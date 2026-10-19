"""子 agent 完成时，往**空闲的**父线程推一条唤醒消息。

父 run 还在跑、或停在 HITL 上一律不动：那两种情况由主管自己的
`AsyncTaskGuard.abefore_agent` / `aafter_agent` 把结果写进 state。本中间件只补
"主管早已答完、没人来拿"这个缺口。

===============================================================================
一、它挂在哪儿、什么时候跑
===============================================================================

本中间件装在**子 agent 的图**里（`graphs/domain_agents.py::graph`），不是主管那边。
所以下面两个 hook 都在**子 run 的进程/协程**里执行：

    aafter_agent      子 agent 正常收尾   -> 摘要 = 子线程最后一条消息
    awrap_model_call  子内模型调用抛异常 -> 只报错，不阻断（异常原样抛回去）

子 agent 写完自己的 checkpoint 就结束，**没有人等它**。它要做的最后一件事是
"通知父线程"，而父线程可能在另一个进程、另一个 worker 上。

===============================================================================
二、返回父 agent 的调用路径（ADR-0005 / ADR-0006）
===============================================================================

为什么不能直接用服务身份打一发 `runs.create`？
因为 aegra 的 `POST /threads/{tid}/runs` 会校验线程属主
（`thread.user_id != user.identity` -> 404）。子 agent 跑在静态服务账号下
（ADR-0004），它写不了属于终端用户的父线程。

所以走"临时借用父身份"：

    拦截层派活时（graphs/async_task_guard.py::_config_for）
      把父上下文塞进子 run 的 config.configurable：
        parent_thread_id / parent_assistant_id /
        parent_user_identity（取服务端注入的 langgraph_auth_user，客户端伪造不了）/
        notify_url（本部署自己的 base url）
            │
            ▼  （子 run 启动，子 agent 干活、收尾）
    aafter_agent
      │ 1. _parent()          从 get_config() 取回上面四项；缺一项 -> 告警并放弃
      │ 2. _mint_parent_token 用 env 的 JWT_SECRET 现铸一枚 300s 的父身份 token
      │                       （不写进 config/DB，用完即弃）
      │ 3. get_client(url=notify_url, headers=Bearer <父token>)
      │                       显式 HTTP 客户端（不是进程内 ASGI）
      │ 4. GET  /threads/{parent}         看父线程忙不忙
      │ 5. POST /custom/tasks/{id}/deliver-claim   抢投递权
      │ 6. POST /threads/{parent}/runs    以父身份开一轮新 run，把结果当 user 消息
      ▼
    父线程多一条 human 消息，内容以 `[Async subagent '<name>' ...]` 开头，
    末尾带 `task_id: <id>` / `run_id: <id>`；主管被这一轮叫醒，转达给用户。
    前端 `web/src/stores/chat.ts::toView` 靠同一个前缀把它渲染成「子 agent 返回」卡片。

===============================================================================
三、多进程下的争夺机制
===============================================================================

部署是 `WORKER_COUNT=2`（.env），所以同一个子 run 的收尾、多个子 run 的收尾、
主管自己的 hook，都可能在不同进程里同时决定"我要投"。三个层各自负责一段：

  层 1  进程内无状态
        本中间件不存任何"投过没"的内存标记（早期版本有一个 set，已删）。
        多进程、多副本下内存标记本来就不可靠，判重完全交给层 2。

  层 2  服务端认领表 async_task_delivery(task_id, run_id)（custom/delivery_api.py）
        主键就是判重键。POST 时在**一个事务**里先拿
        pg_advisory_xact_lock(hashtext(parent_thread_id))——同一父线程的认领串行，
        跨进程也生效——然后：

          主键已存在，且 claimed_at 在 15s 内   -> {"claimed": false, "reason": "delivered"}
          主键已存在，但超过 15s（认领后崩了） -> {"claimed": true,  "reason": "reclaimed"}
          同父线程 8s 内已有别的任务认领       -> {"claimed": false, "reason": "in_flight"}
          其它                                 -> {"claimed": true,  "reason": "claimed"}

        in_flight 那 8 秒是给"同线程同时有多个投递"留的让路窗口：即使任务不同
        （主键不同），也不允许两个投递 run 并发写同一个父线程。

  层 3  state 里的 delivered_run_id（graphs/async_task_guard.py）
        投递成功的任务在 state 上记 `delivered_run_id = run_id`。这是**主管自己**
        那条路的判重依据（包括模型自己 check_async_task 拿到终态结果的情况），
        不需要发 HTTP。

  崩溃语义：认领是"先写票、后投递"。进程在"抢到票"和"真投出去"之间死掉时，
  这次结果 15s 内不会被重投；15s 后过期重投。正常路径下建 run 失败会立刻
  DELETE 撤回，不留死行。

===============================================================================
四、多个子 agent 同时完成
===============================================================================

  ┌─ 子 A 收尾 ─┐      ┌─ 子 B 收尾 ─┐       （A、B 可能在不同进程）
  │ aafter_agent │      │ aafter_agent │
  └──────┬───────┘      └──────┬───────┘
         │ GET 父线程 status            │
         ├──────────────┬──────────────┤
         │              │              │
         ▼              ▼              ▼
   父线程 busy    父线程 idle      父线程 idle
   （还有 run）                  A 先抢到票        B 后到
         │              │              │              │
         ▼              ▼              ▼              ▼
   直接返回，不投    A: runs.create  B: in_flight -> 直接返回
   不认领、不开 run  (新的一轮)      (8s 让路)   等下一次父 run 的 hook 补
         │
         ▼
   结果由父 run 收尾时的
   AsyncTaskGuard.aafter_agent 写进 state，
   不开第二轮 run

注意 in_flight 不是丢结果：B 的结果没被认领、也没被投，但它还在子线程里；
用户下一次说话（或任何新父 run）时，`AsyncTaskGuard.abefore_agent` 会重新扫描
`async_tasks`，发现 B 已终态且 `delivered_run_id` 不等于它的 run_id，就补写一条。

===============================================================================
五、父 agent 内部已经读过这条结果，会怎么处理
===============================================================================

"已经读过"有三个来源，本中间件在每个来源下都不重复投：

  1. 主管的 hook 已经写进 state（同一个 run 的 before_agent / after_agent）
     -> 此时父线程多半已经 idle（那一轮答完了），notifier 仍会走到认领；
        但主键 (task_id, run_id) 已被主管 hook 先抢过 -> 拿到 "delivered" -> 退出。
        （主管 hook 也走同一张表，见 graphs/async_task_guard.py::_deliver）

  2. 模型自己调了 check_async_task 并拿到终态结果
     -> guard 在 awrap_tool_call 里给它记上 delivered_run_id；
        下一轮父 run 的 hook 不会再写，**本中间件也不会补投**。

        为什么不会补投：工具只可能在某个父 run 里被调用，而子 agent 收尾
        （触发本中间件的 aafter_agent）可能早于、也可能晚于调用 check 的那一轮。
        早于的话，那一刻父线程还是 busy -> notifier 已放弃，不会再来第二次；
        晚于的话，用户看到的就是主管自己那一轮把结果用上的回答。两条路上都不产生
        第二张卡片。

        这就是"check 过就没有回调卡片"的代价，也是**有意的**：主管已经在回答里
        用上了结果；再投一条就是让用户收到两份答案（而且新 run 的 input 是一条
        user 消息，模型会再答一遍）。

  3. 同一次子 run 里 aafter_agent 和 awrap_model_call 都触发
     -> 认领表第二次返回 "delivered"，只投一次（只多一次 HTTP）。

===============================================================================
六、失败与兜底
===============================================================================

**只试一次**：没有重试、没有延迟复查。任何异常只记一条 warning。
理由：通知是便利，不是唯一路径。漏掉的结果会在下一次父 run 开始时由
`AsyncTaskGuard.abefore_agent` 补上（它不看认领表过期时间，只看 state）。
代价：父线程一直 busy 且用户不再说话时，这条结果会一直停在子线程里。

===============================================================================
七、类与函数的分布（改代码前先看这张表）
===============================================================================

  A. 准入      _DELIVERABLE, _parent_status()
  B. 内容      _SUMMARY_LIMIT, _shorten(), _extract_last_message()
  C. 传输/身份 _PARENT_TOKEN_TTL, _mint_parent_token(), _claim_endpoint(),
               _claim_delivery(), _release_delivery()
  D. 上下文    CompletionNotifierMiddleware._config() / _parent() / _run_key() / _task_id()
  主干         CompletionNotifierMiddleware._send_notification()
  E. 接线      aafter_agent(), awrap_model_call(), build_completion_notifier()
"""

from __future__ import annotations

import os
import time
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

# 只有这两种线程状态可以投：busy 交给主管 hook，interrupted 不能顶掉用户待答的问题
_DELIVERABLE = frozenset({"idle", "error"})

# 摘要上限：与 graphs/async_task_guard.py 同一口径（同一个截断标记，模型才认得）
_SUMMARY_LIMIT = 500
_TRUNCATED = f"…[结果已截断（仅前 {_SUMMARY_LIMIT} 字），调 check_async_task 取全文]"


# ---------- A. 准入：父线程此刻能不能投 ----------


async def _parent_status(client: Any, thread_id: str) -> str:
    """父线程状态；查不到返回 "unknown"（按不投处理，交给主管 hook 兜底）。"""
    try:
        thread = await client.threads.get(thread_id)
    except Exception:  # noqa: BLE001
        return "unknown"
    return str((thread or {}).get("status") or "idle")


# ---------- B. 内容：投什么 ----------


def _shorten(text: str) -> str:
    """截断摘要并显式留标记——静默硬切会让模型分不清「写完了」和「被切了」。"""
    return text if len(text) <= _SUMMARY_LIMIT else text[:_SUMMARY_LIMIT] + _TRUNCATED


def _extract_last_message(state: dict[str, Any]) -> str:
    """子 agent 的结论 = 它自己 state 的最后一条消息；空对话给占位文案。"""
    messages = state.get("messages", [])
    if not messages:
        return "(no output)"
    last = messages[-1]
    content = getattr(last, "content", None)
    if content is None and isinstance(last, dict):
        content = last.get("content", "")
    return _shorten(content if isinstance(content, str) else str(content))


# ---------- C. 传输与身份：铸 token、认领、撤票 ----------


def _mint_parent_token(identity: str) -> str:
    """现铸一枚短时父身份 token（sub/iat/exp，HS256——custom_auth 要求的三件套）。"""
    now = int(time.time())
    return jwt.encode(
        {"sub": identity, "iat": now, "exp": now + _PARENT_TOKEN_TTL},
        os.environ["JWT_SECRET"],
        algorithm="HS256",
    )


def _claim_endpoint(url: str, task_id: str) -> str:
    """认领接口地址（POST 抢票 / DELETE 撤票共用同一个端点）。"""
    return f"{url.rstrip('/')}/custom/tasks/{task_id}/deliver-claim"


async def _claim_delivery(url: str, token: str, parent_thread_id: str, task_id: str, run_id: str) -> tuple[bool, str]:
    """向服务端认领这次执行结果的投递权（跨进程幂等；见 custom/delivery_api.py）。"""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            _claim_endpoint(url, task_id),
            json={"parent_thread_id": parent_thread_id, "run_id": run_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        payload = resp.json()
    return bool(payload.get("claimed")), str(payload.get("reason") or "")


async def _release_delivery(url: str, token: str, parent_thread_id: str, task_id: str, run_id: str) -> None:
    """撤销认领：投递 run 没创建成功时调用，否则这次结果会被永久判为"已投递"。"""
    async with httpx.AsyncClient(timeout=10) as client:
        await client.delete(
            _claim_endpoint(url, task_id),
            params={"parent_thread_id": parent_thread_id, "run_id": run_id},
            headers={"Authorization": f"Bearer {token}"},
        )


class CompletionNotifierMiddleware(AgentMiddleware):
    """子 agent 完成/出错时，父线程空闲就推一条唤醒消息。

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

    # ---------- D. 上下文：一次解析出父线程四元组 ----------

    @staticmethod
    def _config() -> dict[str, Any]:
        """当前子 run 的 RunnableConfig；图外调（单测/脚本）时返回空 dict。"""
        try:
            return get_config() or {}
        except RuntimeError:  # hook 在图外被触发（测试/脚本）
            return {}

    def _run_key(self) -> str:
        """子 run 的 id——认领表的主键之一，也是写进消息末尾的 `run_id:` 标记。

        拿不到 run_id 时退到 thread_id，再退到常量，保证总能拼出一个键
        （退化的键会让判重变粗，但不会让投递失败）。
        """
        conf = self._config().get("configurable") or {}
        return str(conf.get("run_id") or conf.get("thread_id") or "unknown-run")

    def _task_id(self) -> str | None:
        """子 run 的 thread_id 就是父侧记的 task_id（guard 建任务时两者相同）。"""
        return str((self._config().get("configurable") or {}).get("thread_id") or "") or None

    def _parent(self) -> tuple[str, str, str, str] | None:
        """(thread_id, assistant_id, identity, url)；缺任何一项都放弃回写。

        config 优先（拦截层注入的那套），构造参数回退（官方 factory 模式）。
        """
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

    # ---------- 主干：准入 → 认领 → 投递 → 失败撤票 ----------

    async def _send_notification(self, message: str, task_id: str | None = None) -> None:
        """主干：先看父线程状态，再抢认领，抢到才开 run；任何一步不过就放弃。"""
        parent = self._parent()
        if parent is None:
            return
        thread_id, assistant_id, identity, url = parent
        task_id = task_id or self._task_id() or thread_id
        run_key = self._run_key()
        # 末尾带 task_id / run_id：主管侧判重与这里都靠这两个标记（两边投递只留一份）
        body = f"{message}\n\ntask_id: {task_id}\nrun_id: {run_key}"
        try:
            token = _mint_parent_token(identity)
            client = get_client(url=url, headers={"Authorization": f"Bearer {token}"})
            status = await _parent_status(client, thread_id)
            if status not in _DELIVERABLE:
                logger.info(
                    "completion_notify_skipped_parent_state",
                    parent_thread_id=thread_id,
                    parent_status=status,
                    subagent=self.subagent_name,
                    task_id=task_id,
                )
                return

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
                    input={"messages": [{"role": "user", "content": body}]},
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
            )
        except Exception as e:  # noqa: BLE001 —— 通知是尽力而为，失败要留痕不能吞
            logger.warning(
                "completion_notify_failed",
                parent_thread_id=thread_id,
                subagent=self.subagent_name,
                task_id=task_id,
                error=str(e),
                exc_info=True,
            )

    # ---------- E. 接线：两个 hook ----------

    async def aafter_agent(self, state: dict[str, Any], runtime: Runtime) -> dict[str, Any] | None:
        """子 agent 正常收尾：把结果通知父线程（要不要投由 _send_notification 判）。"""
        summary = _extract_last_message(state)
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
