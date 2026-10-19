"""领域子 agent：**一个图服务所有空间**，按 run config 里的 space 现读 prompt。

aegra.json 注册::

    "domain_agent": "./graphs/domain_agents.py:graph",

调用链：主管 `start_async_task(subagent_type="<space 名>")` → 拦截层
（`graphs/async_task_guard.py`）把 ``configurable.user_spaces = [space]`` 转发给本图 →
这里用那个 space 去 ``graphs/okf/<space>/AGENT.md`` 取 system prompt。

所以**增删空间不改本文件**：空间 = 知识空间下的一个文件夹 + 一行 `space` 表记录。
唯一的图只有这一个，``graph_id`` 固定 ``domain_agent``。

xlsx 四件套按空间里有没 `.xlsx` 决定要不要装（纯文档空间不装，省掉工具 schema 与
CodeInterpreterMiddleware）。

本图是工厂图（1 参数 config）：aegra 对工厂图不缓存、按 run 重建，正好承载
"prompt 取决于这次 run 的 space"。
"""

from __future__ import annotations

from typing import Any

import dotenv
from deepagents import FilesystemPermission, create_deep_agent
from langchain_quickjs import CodeInterpreterMiddleware
from langgraph.graph.state import RunnableConfig

from custom.middlewares.completion_notifier import CompletionNotifierMiddleware
from graphs.llms import model
from graphs.space_access import allowed_spaces
from graphs.space_agents import KNOWLEDGE_ROOT, has_tables, load_agent_spec
from graphs.space_backend import SpaceBackend
from graphs.tools import XLSX_TOOLS

dotenv.load_dotenv()


def _space_of(config: RunnableConfig) -> str:
    """本次 run 绑定的 space（拦截层转发的是单元素列表）；取不到返回空串。"""
    spaces = allowed_spaces(config)
    return next(iter(sorted(spaces)), "") if spaces else ""


def graph(config: RunnableConfig) -> Any:  # noqa: ANN401 — 返回编译后的图
    """建一个只服务 ``config`` 里那个 space 的领域子 agent。"""
    space = _space_of(config)
    _, system_prompt = load_agent_spec(space)

    middleware: list[Any] = []
    if has_tables(space):
        middleware.append(
            CodeInterpreterMiddleware(
                mode="turn",
                ptc=XLSX_TOOLS,  # pyright: ignore[reportArgumentType]
                timeout=120,
                max_result_chars=10000,
            )
        )
    # 完成时主动回写父线程（父上下文从 config 读，ADR-0005 选项1）
    middleware.append(CompletionNotifierMiddleware(subagent_name=space or "domain_agent"))

    return create_deep_agent(
        model=model,
        system_prompt=system_prompt,
        middleware=middleware,
        # 子 agent 同样按 space 隔离：spaces 来自拦截层经 run config 转发的 user_spaces
        backend=SpaceBackend(root_dir=KNOWLEDGE_ROOT),
        permissions=[FilesystemPermission(operations=["write"], paths=["/"], mode="deny")],
    )
