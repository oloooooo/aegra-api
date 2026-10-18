"""兼容垫片：实现已搬到 graphs/tools/xlsx/，这里只做 re-export。

保留原因：`xlsx_process copy.py` 等旧引用不断会炸图；新代码请直接
`from graphs.tools import XLSX_TOOLS`。
"""

from graphs.tools.xlsx import XLSX_TOOLS, xlsx_info, xlsx_profile, xlsx_query, xlsx_search

__all__ = ["XLSX_TOOLS", "xlsx_info", "xlsx_profile", "xlsx_query", "xlsx_search"]
