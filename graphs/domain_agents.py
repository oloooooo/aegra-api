"""3 个领域子 agent 图：同一 builder，按 space 各自实例化。

aegra.json 注册::

    "cbam_reporting":  "./graphs/domain_agents.py:graph_cbam_reporting",
    "hybrid_bonding":  "./graphs/domain_agents.py:graph_hybrid_bonding",
    "sbol3_synbio":    "./graphs/domain_agents.py:graph_sbol3_synbio",

每个图的差异只有 system prompt（graphs/prompts/*.md）、CompletionNotifier
里的名字、以及是否带 xlsx 四件套（有表格的空间：cbam/hybrid_bonding；纯文档
空间：sbol3 不带，也不装 CodeInterpreterMiddleware）。工具/权限/隔离栈：
XLSX_TOOLS 经 CodeInterpreterMiddleware（ptc=程序化工具调用）、SpaceBackend、
写禁用。子 agent 能访问哪个 space 由拦截层（graphs/async_task_guard.py）按 spec
的 space 绑定经 run config 转发。
"""

from deepagents import FilesystemPermission, create_deep_agent
import dotenv
import os

from pathlib import Path
from langchain_quickjs import CodeInterpreterMiddleware

from graphs.llms import model
from graphs.space_backend import SpaceBackend
from graphs.prompts import (
    CBAM_SYSTEM_PROMPT,
    HYBRID_BONDING_SYSTEM_PROMPT,
    SBOL3_SYSTEM_PROMPT,
)
from graphs.tools import XLSX_TOOLS
from custom.middlewares.completion_notifier import CompletionNotifierMiddleware

dotenv.load_dotenv()

okf_dir = str(Path(__file__).resolve().parent / os.getenv("OKF_DIR", "okf"))


def _domain_agent(name: str, system_prompt: str, *, xlsx: bool = True):
    """按 agent 名建一个领域子 agent 图（名字用于回写通知与日志）。

    ``xlsx=False``（纯文档空间）：不带 xlsx 四件套与 CodeInterpreterMiddleware。
    """
    middleware = []
    if xlsx:
        middleware.append(
            CodeInterpreterMiddleware(
                mode="turn",
                ptc=XLSX_TOOLS,  # pyright: ignore[reportArgumentType]
                timeout=120,
                max_result_chars=10000,
            )
        )
    middleware.append(
        # 完成时主动回写父线程（父上下文从 config 读，ADR-0005 选项1）
        CompletionNotifierMiddleware(subagent_name=name)
    )
    return create_deep_agent(
        model=model,
        system_prompt=system_prompt,
        middleware=middleware,
        # 子 agent 同样按 space 隔离：spaces 来自拦截层经 run config 转发的 user_spaces
        backend=SpaceBackend(root_dir=okf_dir),
        permissions=[FilesystemPermission(operations=["write"], paths=["/"], mode="deny")],
    )


graph_cbam_reporting = _domain_agent("cbam_reporting", CBAM_SYSTEM_PROMPT)
graph_hybrid_bonding = _domain_agent("hybrid_bonding", HYBRID_BONDING_SYSTEM_PROMPT)
graph_sbol3_synbio = _domain_agent("sbol3_synbio", SBOL3_SYSTEM_PROMPT, xlsx=False)
