"""tools 包：graphs 下各 agent 用的工具。

- ``xlsx``：查表四件套（主 agent 经子 agent 间接用，子 agent 直接用）。
- ``ask``：人工追问，只给主 agent 用（``MAIN_TOOLS``），子 agent 不装。

用法::

    from graphs.tools import XLSX_TOOLS  # 子 agent / CodeInterpreterMiddleware 用
    from graphs.tools import MAIN_TOOLS  # 主 agent tools=[...MAIN_TOOLS]
"""

from graphs.tools.ask import ASK_TOOLS, ask_user_question
from graphs.tools.xlsx import XLSX_TOOLS, xlsx_info, xlsx_profile, xlsx_query, xlsx_search

# 主 agent 的工具：追问专用。查表活一律派给子 agent，主 agent 不直接装 XLSX_TOOLS。
MAIN_TOOLS = [*ASK_TOOLS]

__all__ = [
    "ASK_TOOLS",
    "MAIN_TOOLS",
    "XLSX_TOOLS",
    "ask_user_question",
    "xlsx_info",
    "xlsx_profile",
    "xlsx_query",
    "xlsx_search",
]
