"""任务结果投递的共享口径：摘要截断 + 投递消息格式（ADR-0010）。

三个地方必须产出**逐字一致**的文本，否则前端卡片认不出、跨路径判重也认不出：

    custom/service/outbox_dispatcher.py   建唤醒 run 时把结果当 input（生产端）
    graphs/middleware/async_task_guard.py    父 run 的 before_agent 写进 state（消费端）
    src/.../run_status.py         子 run 终态时把摘要存进发件箱行

本模块只放纯函数，不导入任何框架或 DB 代码——`graphs/` 在模块加载期就会 import 它。
"""

from __future__ import annotations

from typing import Any

# 投递消息的判重/识别前缀：前端 `web/src/stores/chat.ts::toView` 靠它渲染「子 agent 返回」
PREFIX = "[Async subagent"

# 正文不截断（模型一次就能看到完整结果，不用再调 check_async_task）；超过这个长度只告警，
# 提醒调大上下文 / 拆任务，不静默切文本。
LONG_RESULT_WARN = 3000


def result_body(text: str) -> str:
    """投递正文：原文返回，不截断。"""
    return text


def is_long_result(text: str) -> bool:
    """要不要为“子 agent 结果过长”打一条告警（调用方负责记日志）。"""
    return len(text) > LONG_RESULT_WARN


def task_status_of_run(run: dict[str, Any]) -> str | None:
    """服务端 run → 父 state 里的任务状态。

    `interrupted` 在服务端是一个词，靠 `interrupted_by` 区分两件事：
    - `cancel`（客户端取消 / 被顶掉）→ `cancelled`：这一轮作废，没有结果
    - `hitl`（图停在人工确认点）→ `interrupted`：现场还在
    不映射的话父 state 里 `cancelled` 会被刷成 `interrupted`，同一个状态两个词，
    判重和通知都会错（ADR-0014 §1）。guard 与前端代理共用这一处。
    """
    status = run.get("status")
    if status != "interrupted":
        return status
    return "cancelled" if run.get("interrupted_by") == "cancel" else "interrupted"


def message_text(msg: Any) -> str:
    """消息正文：兼容 str / content 块列表 / dict 三种形态。"""
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content) if content is not None else ""


def completion_text(
    task_id: str,
    run_id: str | None,
    agent_name: str | None,
    status: str | None,
    summary: str,
) -> str:
    """投递消息正文。前端靠前缀识别、判重靠尾部两个标记——两者都不能改。"""
    name = agent_name or "subagent"
    head = (
        f"{PREFIX} '{name}' has completed] Result: {summary}"
        if status == "success"
        else f"{PREFIX} '{name}' has stopped ({status})] Result: {summary}"
    )
    return f"{head}\n\ntask_id: {task_id}\nrun_id: {run_id or ''}"


def notice_text(
    task_id: str,
    run_id: str | None,
    agent_name: str | None,
    status: str | None,
) -> str:
    """状态通知正文：只说"它停了/它被取消了"，**不带 Result**。

    和 `completion_text` 共用 `[Async subagent` 前缀（前端靠它识别成子任务事件），
    但第二段是状态而不是结果——前端据此渲染灰色状态条，而不是「子任务返回结果」卡片。

    `cancelled` 额外带一句“别自己重派”：用户点停止就是不想再跑，而主管被唤醒时
    看到待办还差这一项，会自作主张重新派发（实测过一次：取消后主管 3 秒又派了同名任务）。
    `interrupted`（HITL）不加——那种现场还在，本来就该续。
    """
    name = agent_name or "subagent"
    reason = {"cancelled": "was cancelled", "interrupted": "was interrupted"}.get(status or "", f"stopped ({status})")
    body = f"{PREFIX} '{name}' {reason}]\n\ntask_id: {task_id}\nrun_id: {run_id or ''}"
    if status == "cancelled":
        body += (
            "\n\nThe user cancelled this task, so it will never produce a conclusion."
            " Do not re-dispatch or retry it on your own; if the user's request still"
            " needs it, ask the user first."
        )
    return body
