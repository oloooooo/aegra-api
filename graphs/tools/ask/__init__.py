"""ask 域：主 agent 专用的人工追问工具 + 输入输出 schema。

只有 supervisor（主 agent）装这个工具，子 agent（graphs/space_agent.py 的领域
agent）看不到它——
物理隔离，不在工具里做鉴权。

机制：tool 内调 ``langgraph.types.interrupt`` 抛可恢复中断，图停在工具节点，
payload（问题 + 选项）经 ``__interrupt__`` 浮到调用方；用户答复后调用方用
``Command(resume=...)`` 续跑，答复回到 ``interrupt()`` 的返回值，原样还给模型。
要跑通需要 checkpointer——服务端按请求注入 PostgresSaver，本地裸 ``invoke``
没有 checkpointer 会直接报错（这是预期的：追问只能发生在有记忆的会话里）。
"""

import json
from typing import Any

from langchain.tools import ToolRuntime, tool
from langgraph.types import interrupt

from graphs.tools.ask.schema import AskQuestionArgs, AskUserQuestionOutput, QuestionOption


def _normalize_options(options: Any) -> list[dict[str, Any]]:
    """把模型传的 options（model 实例 / dict / 裸字符串）统一成 label/description dict。"""
    normalized: list[dict[str, Any]] = []
    for opt in options or []:
        if isinstance(opt, QuestionOption):
            normalized.append({"label": opt.label, "description": opt.description})
        elif isinstance(opt, dict):
            normalized.append({"label": str(opt.get("label", "")), "description": opt.get("description")})
        else:
            normalized.append({"label": str(opt), "description": None})
    return normalized


def _format_answer(answer: Any) -> str:
    """resume 值可能是裸字符串，也可能是 {"answer": ...}；统一包成输出 schema 的 JSON。"""
    if isinstance(answer, str):
        selected = answer
    elif isinstance(answer, dict) and "answer" in answer:
        selected = answer["answer"]
    else:
        selected = json.dumps(answer, ensure_ascii=False, default=str)
    if not isinstance(selected, str):
        selected = json.dumps(selected, ensure_ascii=False, default=str)
    return AskUserQuestionOutput(answer=selected).model_dump_json()


@tool(args_schema=AskQuestionArgs)
def ask_user_question(
    question: str,
    options: list[dict[str, Any]],
    header: str | None = None,
    *,
    runtime: ToolRuntime,
) -> str:
    """用户的问题模棱两可时停下来追问（只有主 agent 有这个工具）。

    调用后整个图暂停，问题和选项浮给用户；用户答复后把 {"answer": "..."} 交还给你，
    你再继续派活。不要猜——缺文件路径、分不清查哪个表、指标口径不明时一律先问。

    Args:
        question: 追问的具体问题，以问号结尾，如“你要查的是哪个文件？”
        options: 2-4 个选项，每个 {"label": ..., "description": ...}；
            把你推荐的放第一个。用户也可以打字自由答复。
        header: 极短标签（≤8 字），如“选文件”，缺省不带
    """
    _ = runtime  # Resolver 占位：追问不需要运行时状态，但签名保留注入点
    payload = {
        "tool": "ask_user_question",
        "question": question,
        "header": header,
        "options": _normalize_options(options),
    }
    answer = interrupt(payload)
    return _format_answer(answer)


ASK_TOOLS = [ask_user_question]
