"""xlsx 域共享 schema：四个工具的 args_schema。

tool 装饰器在 ``args_schema=`` 里引用这些模型；模型字段即工具参数，
``Field(description=...)`` 即参数说明（给模型看）。
"""

from typing import Annotated, Optional

from pydantic import BaseModel, Field

# 共享参数别名：path/sheet/header/limit 在四个工具里含义一致，描述只写一遍
XlsxPath = Annotated[str, Field(description="xlsx 虚拟路径，如 /空间名/文件.xlsx（根散文件直接 /文件.xlsx）")]
XlsxSheet = Annotated[Optional[str], Field(description="sheet 名，缺省第一个")]
XlsxHeader = Annotated[int, Field(description="表头所在行号（0 起）。表格上方有标题行时传 1", ge=0)]
XlsxLimit = Annotated[int, Field(description="最多返回行数/命中数", ge=1)]
XlsxOffset = Annotated[int, Field(description="跳过前 N 行（翻页用）", ge=0)]


class XlsxInfoArgs(BaseModel):
    """xlsx_info 的输入。"""

    path: XlsxPath
    sheet: XlsxSheet = None
    header: XlsxHeader = 0


class XlsxQueryArgs(BaseModel):
    """xlsx_query 的输入。"""

    path: XlsxPath
    where: Optional[str] = Field(default=None, description="pandas query 表达式，如 \"数量 > 0\"；不传返回全部行")
    columns: Optional[list[str]] = Field(default=None, description="只返回这些列，缺省返回全部列")
    sheet: XlsxSheet = None
    header: XlsxHeader = 0
    limit: XlsxLimit = 20
    offset: XlsxOffset = 0


class XlsxSearchArgs(BaseModel):
    """xlsx_search 的输入。"""

    path: XlsxPath
    keyword: str = Field(description="要找的文本（字面匹配，不是正则）")
    sheet: XlsxSheet = None
    header: XlsxHeader = 0
    limit: XlsxLimit = 50


class XlsxProfileArgs(BaseModel):
    """xlsx_profile 的输入。"""

    path: XlsxPath
    sheet: XlsxSheet = None
    header: XlsxHeader = 0
