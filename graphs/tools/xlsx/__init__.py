"""xlsx 域：info / query / search / profile 四个只读工具 + 共享 schema。

- 路径是虚拟路径，跟文件工具同一套：``/空间名/x.xlsx`` 是知识空间里的存量文件，
  ``/uploads/x.xlsx`` 是本次会话上传的文件。**字节一律经 backend 取**（``adownload_files``），
  所以空间隔离只有在 ``SpaceGuard`` 一份，工具不再自己判（2026-02 改）。
- 取到的字节先落到本机缓存（按内容 sha 命名，同一份文件只存一份），再走同一套 SQLite 索引；
  每个工具调用都会取一次字节——换来的是“绝不会答旧文件”，代价是一次下载（见 ``store.cache_bytes``）。
- pandas 是同步库，tool 统一 ``async def`` + ``asyncio.to_thread`` 跑同步实现，不卡事件循环；
- 每个 tool 注入 ``runtime: ToolRuntime``，``_run`` 统一用 ``stream_writer`` 打
  start/done/error 关键消息（调用方要开 ``custom`` 流模式才收得到）；
- xlsx 是 zip 二进制，read/grep 读不了，查表一律走这四个工具。

agent 视角::

    xlsx_info(path)                                   # 有哪些 sheet、表头、形状、前几行预览
    xlsx_query(path, where="数量 > 0", columns=[...])  # 按列条件筛行（where 是 SQL 谓词）
    xlsx_search(path, keyword="ABC")                  # 全表撒网找关键词，返回 sheet/行/列
    xlsx_profile(path)                                # 空值/重复/类型/数值范围画像

索引为什么必要：80k×10×3 的表现在一次全读 41.7 s，建好索引后 `WHERE … LIMIT 200` 是 2 ms。
"""

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime, tool

from graphs.access.factory import session_backend
from graphs.tools.xlsx import index, store
from graphs.tools.xlsx.schema import (
    XlsxHeader,
    XlsxInfoArgs,
    XlsxLimit,
    XlsxOffset,
    XlsxPath,
    XlsxProfileArgs,
    XlsxQueryArgs,
    XlsxSearchArgs,
    XlsxSheet,
)


async def _fetch(path: str, runtime: ToolRuntime) -> tuple[Path, str]:
    """虚拟路径 → 本地 xlsx + 索引缓存键：字节经 backend 取，隔离判定只有一份。"""
    backend = session_backend()
    resp = (await backend.adownload_files([path]))[0]
    if resp.error is not None or resp.content is None:
        raise FileNotFoundError(f"读不到文件：{path}（{resp.error or '内容为空'}）")
    return await asyncio.to_thread(store.cache_bytes, path, resp.content)


# ---------- 同步实现（跑在线程池里） ----------


def _conn(xlsx: Path, cache_key: str, header: int) -> sqlite3.Connection:
    """本地 xlsx → 索引 → 只读连接（同步，跑在线程池里）。"""
    return index.open_ro(index.ensure(xlsx, store.cache_dir(), cache_key, header))


# ---------- 同步实现（跑在线程池里） ----------


def _info(xlsx: Path, cache_key: str, name: str, sheet: str | None, header: int) -> str:
    with closing(_conn(xlsx, cache_key, header)) as conn:
        return index.info(conn, name, sheet)


def _query(
    xlsx: Path,
    cache_key: str,
    _name: str,
    sheet: str | None,
    header: int,
    columns: list[str] | None,
    where: str | None,
    limit: int,
    offset: int,
) -> str:
    with closing(_conn(xlsx, cache_key, header)) as conn:
        return index.query(conn, sheet, columns, where, limit, offset)


def _search(
    xlsx: Path, cache_key: str, _name: str, keyword: str, sheet: str | None, header: int, limit: int
) -> str:
    with closing(_conn(xlsx, cache_key, header)) as conn:
        return index.search(conn, keyword, sheet, header, limit)


def _profile(xlsx: Path, cache_key: str, _name: str, sheet: str | None, header: int) -> str:
    with closing(_conn(xlsx, cache_key, header)) as conn:
        return index.profile(conn, sheet)


# ---------- 异步 tool（agent 用这一层） ----------


async def _run(name: str, fn: Any, runtime: ToolRuntime, /, path: str, *args: Any) -> str:
    """取字节 → 同步实现丢线程池；异常转成错误文本返回，让 agent 能自我纠正而不是炸图。"""
    writer = runtime.stream_writer
    writer({"tool": name, "event": "start", "args": [path, *args]})
    try:
        xlsx, cache_key = await _fetch(path, runtime)
        result = await asyncio.to_thread(fn, xlsx, cache_key, store.display_name(path), *args)
    except Exception as e:  # noqa: BLE001 —— 故意兜底，错误信息是给 agent 看的
        writer({"tool": name, "event": "error", "error": f"{type(e).__name__}: {e}"})
        return f"错误：{type(e).__name__}: {e}"
    writer({"tool": name, "event": "done"})
    return result


@tool(args_schema=XlsxInfoArgs)
async def xlsx_info(path: XlsxPath, sheet: XlsxSheet = None, header: XlsxHeader = 0, *, runtime: ToolRuntime) -> str:
    """查看 xlsx 概况：有哪些 sheet、每个 sheet 的行数和列名、前 3 行预览。

    Args:
        path: xlsx 虚拟路径，如 /空间名/文件.xlsx；会话文件用 /uploads/文件名.xlsx
        sheet: 要预览的 sheet 名，缺省预览第一个
        header: 表头所在行号（0 起）。表格上方有标题行时传 1
    """
    return await _run("xlsx_info", _info, runtime, path, sheet, header)


@tool(args_schema=XlsxQueryArgs)
async def xlsx_query(
    path: XlsxPath,
    where: str | None = None,
    columns: list[str] | None = None,
    sheet: XlsxSheet = None,
    header: XlsxHeader = 0,
    limit: XlsxLimit = 20,
    offset: XlsxOffset = 0,
    *,
    runtime: ToolRuntime,
) -> str:
    """按条件查 xlsx 的行，返回 CSV 文本。适合"已知列名"的查询。

    `where` 是 **SQL 谓词**（不是 pandas 表达式），三条差异必须注意：
    1. 列名含空格/括号/中文以外的符号时用双引号包起来：`"价 格" > 0`；
    2. 空值用 `IS NULL` / `IS NOT NULL`，**`!=` 会漏掉空值行**（SQL 里 NULL 参与比较不成立）；
    3. 不支持 `@变量`、`.str.xxx`、`~` 取反这类 pandas 写法。
    其余（`> < >= <= = != and or in between like`）照常。

    Args:
        path: xlsx 虚拟路径（/空间名/文件.xlsx 或 /uploads/文件名.xlsx）
        where: SQL 谓词，如 "数量 > 0"、"料号 = 'ABC123'"、"a > 1 AND b = 'x'"；不传则全表
        columns: 只返回这些列，缺省返回全部列（宽表建议指定）
        sheet: sheet 名，缺省第一个
        header: 表头所在行号（0 起）
        limit: 最多返回行数（单次上限 200）
        offset: 跳过前 N 行（翻页用）
    """
    return await _run("xlsx_query", _query, runtime, path, sheet, header, columns, where, limit, offset)


@tool(args_schema=XlsxSearchArgs)
async def xlsx_search(
    path: XlsxPath,
    keyword: str,
    sheet: XlsxSheet = None,
    header: XlsxHeader = 0,
    limit: XlsxLimit = 50,
    *,
    runtime: ToolRuntime,
) -> str:
    """全表搜索关键词（字面匹配，不区分大小写），返回命中的 sheet/行/列/值。

    适合"不知道关键词在哪列"的排查；某一列本来就反复出现的值（如 site=1）不要用它，
    改用 `xlsx_query` 按列筛。命中按**单元格**计，一行两列都命中会出两条。

    Args:
        path: xlsx 虚拟路径（/空间名/文件.xlsx 或 /uploads/文件名.xlsx）
        keyword: 要找的文本（字面匹配，不是正则，`%`/`_` 按普通字符处理）
        sheet: 只搜这个 sheet，缺省搜全部 sheet
        header: 表头所在行号（0 起）
        limit: 最多返回命中数（单次上限 100）
    """
    return await _run("xlsx_search", _search, runtime, path, keyword, sheet, header, limit)


@tool(args_schema=XlsxProfileArgs)
async def xlsx_profile(path: XlsxPath, sheet: XlsxSheet = None, header: XlsxHeader = 0, *, runtime: ToolRuntime) -> str:
    """数据质量画像：每列的空值数、唯一值数、数值范围（或文本样例），以及重复行数。

    Args:
        path: xlsx 虚拟路径（/空间名/文件.xlsx 或 /uploads/文件名.xlsx）
        sheet: sheet 名，缺省第一个
        header: 表头所在行号（0 起）
    """
    return await _run("xlsx_profile", _profile, runtime, path, sheet, header)


def _expose_model_schema(t: Any) -> Any:
    """让 args_schema 用模型可见的那份（tool_call_schema）。

    langchain-quickjs 用 args_schema 渲染 JS 里的 tools.x 签名，而带 ``runtime: ToolRuntime``
    的 args_schema 生成不出 JSON Schema（CallableSchema），会退化成
    ``(input: Record<string, unknown>)``，模型就瞎传 ``{input: "xx.xlsx"}`` 而报
    "path: Field required"。换掉后签名正常，runtime 仍按函数签名注入。
    """
    t.args_schema = t.tool_call_schema
    return t


XLSX_TOOLS = [_expose_model_schema(t) for t in (xlsx_info, xlsx_query, xlsx_search, xlsx_profile)]
