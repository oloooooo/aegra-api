"""每个 Space 就是一个子 agent：目录、``AGENT.md``、建空间时的脚手架。

这是「空间 → 子 agent」的**单一来源**：图工厂（`graphs/space_agent.py`、
`graphs/supervisor.py`）与建空间接口（`custom/api/spaces.py`）都从这里取，
不再有 ``_DOMAIN_SPECS`` 那样的硬编码名单。加/删一个空间 = 加/删一个文件夹 + 一行表。

**两种存放**（`okf.backend`，ADR-0016）：``local`` 读本地盘 ``<okf.dir>/<space>``；
``s3fs`` 读对象存储 ``okf/<space>/``。本模块把这层差异吃在自己内部，上面那些调用方
（图工厂、HTTP 路由）看到的仍是同一组函数：空间存在吗、有哪些文件、AGENT.md 写了什么。

本模块刻意**不 import** deepagents / langchain：HTTP 路由会用到它，别把整张图拖进启动路径。

``AGENT.md`` 的格式（frontmatter 可选，只认单行 ``key: value``）::

    ---
    description: 一句话说明这个空间能回答什么（主管据此决定派给谁）
    ---
    你是 <space> 空间的专用知识 agent……（正文 = 子 agent 的 system prompt）

缺文件或缺字段都有兜底：description 退到正文首行，prompt 退到一句通用说明。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared import config
from shared.knowledge.root import backend_mode, knowledge_root
from shared.session import blobs

# 项目根（= 仓库根）。space.path 存的相对路径以它为基准（frontend_api 也这么解）
PROJECT_ROOT = config.ROOT

AGENT_FILE = "AGENT.md"
_SPACE_PREFIX = "okf/"

# 空间名直接当子 agent 名（deepagents 只把它当字符串用），所以必须能进工具参数与日志：
# 不许路径分隔符、不许以点开头、不许空
_NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}$")

_DEFAULT_PROMPT = """你是 {space} 空间的专用知识 agent，只读不写。主 agent 转述的任务会带 /{space}/xxx 这样的虚拟路径。

## 知识库（先读后答）
先 read /{space}/index.md 摸清这个空间有哪些文档，回答问题前必读对应文档，不要凭记忆答。

## 流程
最终答案 = 结论 + 出处（文档名或表/行），不要贴超长原表。
"""


def valid_space_name(name: str) -> bool:
    """空间名合法？（同时是目录名与子 agent 名，一个规则管住两处）"""
    return bool(_NAME_RE.match(name or "")) and name not in {".", ".."}


def _remote() -> bool:
    """空间内容在对象存储里（`okf.backend: s3fs`，没有本地目录）。"""
    return backend_mode() == "s3fs"


def space_dir(space: str) -> Path:
    """空间对应的本地目录（只有 local 模式有；s3fs 模式调用到这里就报错）。"""
    return knowledge_root() / space


def space_exists(space: str) -> bool:
    """空间存在吗？（有内容就算存在——对象存储没有空目录）"""
    if _remote():
        return blobs.exists_prefix(f"{_SPACE_PREFIX}{space}/")
    return space_dir(space).is_dir()


def space_rel_path(space: str) -> str:
    """写进 ``space.path`` 的相对路径（相对项目根，frontend_api 用它浏览文件）。

    只看配置不看当前模式：这个值会入库，不能随部署形态（local / s3fs）变成另一个值。
    统一用 POSIX 分隔符，不随构建平台变反斜线。
    """
    return f"{str(config.get('okf.dir')).strip('/')}/{space}"


def space_files(space: str) -> list[dict[str, Any]]:
    """空间内全部文件（递归）：`[{path, size, modified, ext}]`，path 以 / 开头。

    阻塞 IO：调用方必须 `asyncio.to_thread`。
    """
    if _remote():
        prefix = f"{_SPACE_PREFIX}{space}/"
        return [
            {
                "path": "/" + key[len(prefix) :],
                "size": meta["size"],
                "modified": _iso(meta["modified"]),
                "ext": Path(key).suffix.lower(),
            }
            for key, meta in sorted(blobs.list_tree(prefix).items())
        ]
    root = space_dir(space)
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue  # 遍历期间被删除/改名：跳过，不要让整个文件列表 500
        out.append(
            {
                "path": "/" + p.relative_to(root).as_posix(),
                "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
                "ext": p.suffix.lower(),
            }
        )
    return out


def space_file_count(space: str) -> int:
    """空间内文件数（阻塞 IO，调用方 `asyncio.to_thread`）。"""
    if _remote():
        return len(blobs.list_tree(f"{_SPACE_PREFIX}{space}/"))
    root = space_dir(space)
    return sum(1 for p in root.rglob("*") if p.is_file()) if root.is_dir() else 0


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return ""


def _read_text(space: str, name: str) -> str | None:
    """读空间里的一个文本文件；不存在/读不了返回 None（调用方都有兜底）。"""
    if _remote():
        got = blobs.get_object(f"{_SPACE_PREFIX}{space}/{name}")
        return got[0].decode("utf-8", "replace") if got else None
    try:
        return (space_dir(space) / name).read_text(encoding="utf-8")
    except OSError:
        return None


def _child_names(space: str) -> list[str]:
    """空间**直接子项**名（不递归，与磁盘版 ``glob('*')`` / ``iterdir()`` 同语义）。"""
    if _remote():
        prefix = f"{_SPACE_PREFIX}{space}/"
        return sorted({key[len(prefix) :].split("/", 1)[0] for key in blobs.list_tree(prefix)})
    try:
        return [p.name for p in space_dir(space).iterdir()]
    except OSError:
        return []

def parse_agent_md(text: str) -> tuple[dict[str, str], str]:
    """拆 frontmatter 与正文；没有 frontmatter 时返回 ({}, 原文)。"""
    if not text.startswith("---"):
        return {}, text.strip()
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()
    meta: dict[str, str] = {}
    for line in parts[1].strip().splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, parts[2].strip()


def load_agent_spec(space: str) -> tuple[str, str]:
    """(description, system_prompt)：读 ``<space>/AGENT.md``，缺什么补什么。

    子 agent 派活与主管选人都靠这两个值，所以永远返回可用内容，绝不抛异常——
    空间刚建、AGENT.md 还没写时也要能跑（用兜底文案）。
    """
    if text := _read_text(space, AGENT_FILE):
        meta, body = parse_agent_md(text)
    else:
        meta, body = {}, ""
    description = meta.get("description") or _first_line(body) or f"{space} 空间的领域知识 agent"
    prompt = body or _DEFAULT_PROMPT.format(space=space)
    return description, prompt


def has_tables(space: str) -> bool:
    """空间里有没有 .xlsx：有才给子 agent 装 xlsx 四件套（省掉纯文档空间的工具 schema）。"""
    return any(name.lower().endswith(".xlsx") for name in _child_names(space))


def list_spaces() -> list[str]:
    """知识空间根下所有像空间的目录（本地 demo / 无鉴权上下文时用）。

    只认有 ``AGENT.md`` 或 ``index.md`` 的目录：根下还有装演示数据/散文件的目录
    （result1、secret 之类），它们不是空间。
    """
    if _remote():
        tree = blobs.list_tree(_SPACE_PREFIX)
        names = {key[len(_SPACE_PREFIX) :].split("/", 1)[0] for key in tree}
        return sorted(n for n in names if n and not n.startswith(".") and _looks_like_space(n, tree))
    try:
        return sorted(
            p.name
            for p in knowledge_root().iterdir()
            if p.is_dir() and not p.name.startswith(".") and ((p / AGENT_FILE).exists() or (p / "index.md").exists())
        )
    except OSError:
        return []


def _looks_like_space(name: str, tree: dict[str, Any]) -> bool:
    return any(f"{_SPACE_PREFIX}{name}/{marker}" in tree for marker in (AGENT_FILE, "index.md"))


def scaffold_space(space: str) -> None:
    """建空间 + ``AGENT.md``（已存在的不动，避免覆盖别人写好的 prompt）。"""
    if _remote():
        key = f"{_SPACE_PREFIX}{space}/{AGENT_FILE}"
        try:
            blobs.put_bytes(key, _TEMPLATE.format(space=space).encode(), if_none_match="*")
        except Exception as e:  # noqa: BLE001 —— 已存在（412）不算错，其他错误往上抛
            if "PreconditionFailed" not in type(e).__name__ and "412" not in str(e):
                raise
        return
    folder = space_dir(space)
    folder.mkdir(parents=True, exist_ok=True)
    agent_md = folder / AGENT_FILE
    if not agent_md.exists():
        agent_md.write_text(_TEMPLATE.format(space=space), encoding="utf-8")


def _first_line(text: str) -> str:
    """正文首行（去掉标题井号），当作没写 frontmatter 时的 description。"""
    for line in text.splitlines():
        clean = line.strip().lstrip("#").strip()
        if clean:
            return clean[:200]
    return ""


_TEMPLATE = """---
description: {space} 空间的领域知识 agent——（改成一句话：这个空间能回答什么问题）
---

你是 {space} 空间的专用知识 agent，只读不写。主 agent 转述的任务会带 /{space}/xxx 这样的虚拟路径。

## 知识库（先读后答）
- /{space}/index.md：包总览与导航（没有就列一下这个空间的主要文档）

## 工具
- read：读 .md 知识文档，回答业务逻辑问题必读对应文档，不要凭记忆答。
- xlsx_info / xlsx_query / xlsx_search / xlsx_profile：空间里有 .xlsx 时可用（先 info 认表再 query 取数）。

## 流程
1. 业务规则问题 → 先 read 对应 .md，引用规则时给文档出处。
2. 表/数据问题 → xlsx_info 认表后 xlsx_query 取数，给行/列/单位。
3. 最终答案 = 结论 + 出处，不要贴超长原表。
"""
