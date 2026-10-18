"""xlsx 域：info / query / search / profile 四个只读工具 + 共享 schema。

- 路径以 ``/`` 开头当虚拟路径，解析到 OKF 根（``OKF_DIR``，缺省 graphs/okf，与
  FilesystemBackend 同一根）下，越界报错；相对路径按 cwd 解析；
- pandas 是同步库，tool 统一 ``async def`` + ``asyncio.to_thread`` 跑同步实现，不卡事件循环；
- 每个 tool 注入 ``runtime: ToolRuntime``，``_run`` 统一用 ``stream_writer`` 打
  start/done/error 关键消息（调用方要开 ``custom`` 流模式才收得到）；
- xlsx 是 zip 二进制，read/grep 读不了，查表一律走这四个工具。

agent 视角::

    xlsx_info(path)                                # 有哪些 sheet、表头、形状、前几行预览
    xlsx_query(path, where="数量 > 0", columns=[...])  # 按列条件筛行
    xlsx_search(path, keyword="ABC")               # 全表撒网找关键词，返回 sheet/行/列
    xlsx_profile(path)                             # 空值/重复/类型/数值范围画像
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pandas as pd
from langchain.tools import ToolRuntime, tool

from graphs.space_access import allowed_spaces
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

_MAX_ROWS = 200  # query 单次最多返回行数
_MAX_HITS = 100  # search 单次最多返回命中数
_MAX_VALUE_LEN = 80  # search 命中值截断长度


def _root() -> Path:
    """与 FilesystemBackend 同一根：虚拟路径 /a.xlsx 落到 <okf>/a.xlsx。"""
    # __file__ = <仓库>/graphs/tools/xlsx/__init__.py → parents[2] = <仓库>/graphs，再拼 OKF_DIR（缺省 okf）
    return (Path(__file__).resolve().parents[2] / os.getenv("OKF_DIR", "okf")).resolve()


def _resolve(path: str | Path) -> Path:
    """绝对路径当虚拟路径：/kb/a.xlsx、/a.xlsx 都相对 OKF 根解析，不允许越界。"""
    # 下面每一步都以 agent 传进来的 path = "/消融实验1.xlsx" 为例
    raw = str(path).strip()  # "/消融实验1.xlsx"（顺带干掉首尾空格）
    p = Path(raw)  # WindowsPath('/消融实验1.xlsx')；注意它 is_absolute() == False，所以下一行得自己判
    if raw.startswith(("/", "\\")):  # 首字符是 / 或 \ 就当虚拟路径，不能按 cwd 去解析
        rel = raw.lstrip("/\\")  # "消融实验1.xlsx"
        root = _root()  # <仓库>/graphs/okf
        p = (root / rel).resolve()  # <仓库>/graphs/okf/消融实验1.xlsx；resolve 同时展开 .. ，防止绕过
        if root not in p.parents:  # "/../pyproject.toml" → <仓库>/graphs/pyproject.toml，父目录里没有 okf
            raise ValueError(f"路径越界：{path}")  # 报错文本回给 agent，它会换成合法路径重试
    # 相对路径分支（如 graphs/okf/消融实验1.xlsx）不查越界——那是调用方自己的 cwd
    if not p.is_file():  # 目录 / 拼错 / 文件真不在，都在这里挂
        raise FileNotFoundError(f"文件不存在：{p}")  # 带上完整解析结果，方便定位
    return p  # 给 pd.read_excel 用


def _check_scope(path: str | Path, config: object) -> None:
    """空间隔离（ADR-0005）：XLSX_TOOLS 不经 backend（自己 openpyxl 读盘），
    必须在 tool 层拦：虚拟路径第一段 ∈ allowed spaces，或根散文件共享只读。
    无鉴权上下文（本地 demo）不设限；相对路径在受限时直接拒绝（它绕开 OKF 根）。
    """
    spaces = allowed_spaces(config)  # type: ignore[arg-type]
    if spaces is None:
        return
    raw = str(path).strip()
    if not raw.startswith(("/", "\\")):
        raise ValueError(f"受限模式下必须用以 / 开头的虚拟路径（/空间名/文件.xlsx）：{path}")
    rel = raw.lstrip("/\\")
    first = rel.split("/", 1)[0]
    if first in spaces:
        return  # 文件是否存在由 _resolve 报
    if "/" not in rel and (_root() / first).is_file():
        return  # 根散文件共享只读（Q3b）
    visible = sorted(spaces) or ["（无）"]
    raise ValueError(f"无权访问 {path}：{first} 不在你的空间列表 {visible} 内")


def _load(path: str, sheet: str | None, header: int) -> pd.DataFrame:
    """读单个 sheet（sheet 缺省 = 第一个；注意 sheet_name=None 在 pandas 里是"全部"）。"""
    return pd.read_excel(_resolve(path), sheet_name=sheet if sheet is not None else 0, header=header)


def _cell(v: Any) -> Any:
    """NaN -> None（JSON null），其余原样。"""
    return None if pd.isna(v) else v


# ---------- 同步实现（跑在线程池里） ----------

def _info(path: str, sheet: str | None, header: int) -> str:
    real = _resolve(path)
    sheets = pd.read_excel(real, sheet_name=None, header=header)
    out: dict[str, Any] = {
        "file": real.name,
        "sheets": {name: {"rows": len(df), "columns": [str(c) for c in df.columns]}
                   for name, df in sheets.items()},
    }
    preview_name = sheet if sheet in sheets else next(iter(sheets))
    pv = sheets[preview_name].head(3)
    out["preview"] = {
        "sheet": preview_name,
        "head": [{str(c): _cell(v) for c, v in row.items()}
                 for row in pv.to_dict("records")],
    }
    return json.dumps(out, ensure_ascii=False, default=str)


def _query(path: str, sheet: str | None, header: int, columns: list[str] | None,
           where: str | None, limit: int, offset: int) -> str:
    df = _load(path, sheet, header)
    if where:
        df = df.query(where, engine="python")
    total = len(df)
    if columns:
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise ValueError(f"列不存在：{missing}，可用列：{[str(c) for c in df.columns]}")
        df = df[columns]
    # limit/offset 是 agent 传的，负数会经 iloc 切片漏出全表（如 limit=-1 -> iloc[0:-1]），先钳正
    limit = max(1, min(limit, _MAX_ROWS))
    offset = max(0, offset)
    df = df.iloc[offset:offset + min(limit, _MAX_ROWS)]
    head = f"匹配 {total} 行，返回第 {offset + 1}-{offset + len(df)} 行（列：{[str(c) for c in df.columns]}）"
    return head + "\n" + df.to_csv(index=False)


def _search(path: str, keyword: str, sheet: str | None, header: int, limit: int) -> str:
    if not keyword:
        raise ValueError("keyword 不能为空")
    sheets: dict[str, pd.DataFrame] = pd.read_excel(_resolve(path), sheet_name=None, header=header)
    if sheet is not None:
        if sheet not in sheets:
            raise ValueError(f"sheet 不存在：{sheet}，可用：{list(sheets)}")
        sheets = {sheet: sheets[sheet]}
    kw = keyword.lower()
    # 先扫完全部 sheet 拿真实命中总数——否则截断到 100 条后 agent 会以为总共只有 100 处
    scanned: list[tuple[str, pd.DataFrame, pd.DataFrame, pd.DataFrame]] = []
    total = 0
    for name, df in sheets.items():
        str_df = df.astype(object).astype(str)
        mask = str_df.apply(lambda col: col.str.lower().str.contains(kw, regex=False)) & df.notna()
        total += int(mask.to_numpy().sum())
        scanned.append((name, df, str_df, mask))
    hits: list[str] = []
    for name, df, str_df, mask in scanned:
        if len(hits) >= min(limit, _MAX_HITS):
            break
        for r, c in zip(*mask.to_numpy().nonzero()):
            value = str(str_df.iat[r, c])[:_MAX_VALUE_LEN]
            hits.append(f"{name} | Excel第{r + header + 2}行 | {df.columns[c]} | {value}")
            if len(hits) >= min(limit, _MAX_HITS):
                break
    if not hits:
        return f"未找到包含 {keyword!r} 的单元格"
    truncated = f"（仅显示前 {len(hits)} 条，缩小关键词或改用 xlsx_query 按列取数）" if total > len(hits) else ""
    return f"命中 {total} 处{truncated}（sheet | 行 | 列 | 值）：\n" + "\n".join(hits)


def _profile(path: str, sheet: str | None, header: int) -> str:
    df = _load(path, sheet, header)
    cols: dict[str, Any] = {}
    for col in df.columns:
        s = df[col]
        info: dict[str, Any] = {
            "dtype": str(s.dtype),
            "nulls": int(s.isna().sum()),
            "unique": int(s.nunique()),
        }
        if pd.api.types.is_numeric_dtype(s):
            info["min"] = _cell(s.min())
            info["max"] = _cell(s.max())
            info["mean"] = round(float(s.mean()), 4) if s.notna().any() else None
        else:
            info["sample"] = s.dropna().astype(str).unique()[:5].tolist()
        cols[str(col)] = info
    out = {"rows": len(df), "duplicate_rows": int(df.duplicated().sum()), "columns": cols}
    return json.dumps(out, ensure_ascii=False, default=str)


# ---------- 异步 tool（agent 用这一层） ----------

async def _run(name: str, fn: Any, runtime: ToolRuntime, /, *args: Any) -> str:
    """同步实现丢线程池；异常转成错误文本返回，让 agent 能自我纠正而不是炸图。"""
    writer = runtime.stream_writer
    writer({"tool": name, "event": "start", "args": list(args)})
    try:
        _check_scope(args[0], runtime.config)  # 四个 tool 的第一个参数都是 path
        result = await asyncio.to_thread(fn, *args)
    except Exception as e:  # noqa: BLE001 —— 故意兜底，错误信息是给 agent 看的
        writer({"tool": name, "event": "error", "error": f"{type(e).__name__}: {e}"})
        return f"错误：{type(e).__name__}: {e}"
    writer({"tool": name, "event": "done"})
    return result


@tool(args_schema=XlsxInfoArgs)
async def xlsx_info(path: XlsxPath, sheet: XlsxSheet = None, header: XlsxHeader = 0,
                    *, runtime: ToolRuntime) -> str:
    """查看 xlsx 概况：有哪些 sheet、每个 sheet 的行数和列名、前 3 行预览。

    Args:
        path: xlsx 虚拟路径，如 /空间名/文件.xlsx（根散文件直接 /文件.xlsx）
        sheet: 要预览的 sheet 名，缺省预览第一个
        header: 表头所在行号（0 起）。表格上方有标题行时传 1
    """
    return await _run("xlsx_info", _info, runtime, path, sheet, header)


@tool(args_schema=XlsxQueryArgs)
async def xlsx_query(path: XlsxPath, where: str | None = None, columns: list[str] | None = None,
                     sheet: XlsxSheet = None, header: XlsxHeader = 0,
                     limit: XlsxLimit = 20, offset: XlsxOffset = 0, *, runtime: ToolRuntime) -> str:
    """按条件查 xlsx 的行，返回 CSV 文本。适合"已知列名"的查询。

    Args:
        path: xlsx 虚拟路径
        where: pandas query 表达式，如 "数量 > 0"、"料号 == 'ABC123'"、
               "col_a > 1 and col_b == 'x'"；列名含空格/特殊字符用反引号包裹；
               不传则返回全部行
        columns: 只返回这些列，缺省返回全部列（宽表建议指定）
        sheet: sheet 名，缺省第一个
        header: 表头所在行号（0 起）
        limit: 最多返回行数（单次上限 200）
        offset: 跳过前 N 行（翻页用）
    """
    return await _run("xlsx_query", _query, runtime, path, sheet, header, columns, where, limit, offset)


@tool(args_schema=XlsxSearchArgs)
async def xlsx_search(path: XlsxPath, keyword: str, sheet: XlsxSheet = None,
                      header: XlsxHeader = 0, limit: XlsxLimit = 50, *, runtime: ToolRuntime) -> str:
    """全表搜索关键词（不区分大小写），返回命中的 sheet/行/列/值。适合"不知道关键词在哪列,不适合查询某一注定重复的关键字例如：
       | site  | pn |pn_price  |   usages  |
       | 1     |  4322-111  | 124231 | 3333 |
       | 1     |  4322-112  | 124232 | 33433 |
       | 1     |  4322-113  | 124231 | 33323543 |
       | 1     |  4322-114  | 1224231 | 33333135345 |
       | 1     |  4322-111  | 124231 | 3333 |
       |....|  | ....  | ...|...|...|...|
    site 1 如果表很大就不适合使用关键字全部查询，因为它作为1列重复了许多次。
    Args:
        path: xlsx 虚拟路径
        keyword: 要找的文本（字面匹配，不是正则）
        sheet: 只搜这个 sheet，缺省搜全部 sheet
        header: 表头所在行号（0 起）
        limit: 最多返回命中数（单次上限 100）
    """
    return await _run("xlsx_search", _search, runtime, path, keyword, sheet, header, limit)


@tool(args_schema=XlsxProfileArgs)
async def xlsx_profile(path: XlsxPath, sheet: XlsxSheet = None, header: XlsxHeader = 0,
                       *, runtime: ToolRuntime) -> str:
    """数据质量画像：每列的空值数、唯一值数、数值范围（或文本样例），以及重复行数。

    Args:
        path: xlsx 虚拟路径
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
