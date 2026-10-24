"""任务结果投递的共享口径：摘要截断 + 投递消息格式（ADR-0010）。

三个地方必须产出**逐字一致**的文本，否则前端卡片认不出、跨路径判重也认不出：

    custom/outbox_dispatcher.py   建唤醒 run 时把结果当 input（生产端）
    graphs/async_task_guard.py    父 run 的 before_agent 写进 state（消费端）
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

# **只给发件箱 `summary` 列用**（现查子线程失败时的兜底值）：DB 行没理由存几 MB 文本，
# 而这一列目前恒为 "(no output)"（子 run 不流 values，ADR-0010 已知问题 #3）。
SUMMARY_LIMIT = 500


def shorten(text: str) -> str:
    """截断并显式留标记——静默硬切会让模型分不清「写完了」和「被切了」。

    只用于发件箱 `summary` 列的兜底值；投递正文用 `result_body()`，不截断。
    """
    return text if len(text) <= SUMMARY_LIMIT else text[:SUMMARY_LIMIT] + f"…[截断，仅前 {SUMMARY_LIMIT} 字]"


def result_body(text: str) -> str:
    """投递正文：原文返回，不截断。"""
    return text


def is_long_result(text: str) -> bool:
    """要不要为“子 agent 结果过长”打一条告警（调用方负责记日志）。"""
    return len(text) > LONG_RESULT_WARN


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


def summary_from_output(output: Any) -> str:
    """子 run 的结论 = 它终态时最后一条消息（截断后）；拿不到给占位。

    只用于发件箱 `summary` 列的兜底值，所以留截断；投递正文不截断。
    """
    messages = output.get("messages") if isinstance(output, dict) else None
    if not messages:
        return "(no output)"
    return shorten(message_text(messages[-1]))


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
