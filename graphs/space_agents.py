"""每个 Space 就是一个子 agent：目录、``AGENT.md``、建空间时的脚手架。

这是「空间 → 子 agent」的**单一来源**：图工厂（`graphs/domain_agents.py`、
`graphs/deepagents_nodes.py`）与建空间接口（`custom/routers.py`）都从这里取，
不再有 ``_DOMAIN_SPECS`` 那样的硬编码名单。加/删一个空间 = 加/删一个文件夹 + 一行表。

本模块刻意**不 import** deepagents / langchain：HTTP 路由会用到它，别把整张图拖进启动路径。

``AGENT.md`` 的格式（frontmatter 可选，只认单行 ``key: value``）::

    ---
    description: 一句话说明这个空间能回答什么（主管据此决定派给谁）
    ---
    你是 <space> 空间的专用知识 agent……（正文 = 子 agent 的 system prompt）

缺文件或缺字段都有兜底：description 退到正文首行，prompt 退到一句通用说明。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from graphs.okf_mirror import knowledge_root

# 项目根（= 仓库根）。space.path 存的相对路径以它为基准（frontend_api 也这么解）
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 知识空间根：`OKF_BACKEND=local` 是 `graphs/okf`，`=s3` 是 S3 的本地镜像（ADR-0016）。
# 保留 `KNOWLEDGE_ROOT` 这个常量名给老调用点，真正的解析统一走 `okf_mirror.knowledge_root()`。
KNOWLEDGE_ROOT = Path(__file__).resolve().parent / os.getenv("OKF_DIR", "okf")

AGENT_FILE = "AGENT.md"

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


def space_dir(space: str) -> Path:
    """空间对应的目录：知识空间根下的同名文件夹（s3 模式下是本地镜像）。"""
    return knowledge_root() / space


def space_rel_path(space: str) -> str:
    """写进 ``space.path`` 的相对路径（相对项目根，frontend_api 用它浏览文件）。

    统一用 POSIX 分隔符：这个值会入库，不能随构建平台变成反斜线。
    """
    return Path(os.path.relpath(space_dir(space), PROJECT_ROOT)).as_posix()

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
    path = space_dir(space) / AGENT_FILE
    try:
        meta, body = parse_agent_md(path.read_text(encoding="utf-8"))
    except OSError:
        meta, body = {}, ""
    description = meta.get("description") or _first_line(body) or f"{space} 空间的领域知识 agent"
    prompt = body or _DEFAULT_PROMPT.format(space=space)
    return description, prompt


def has_tables(space: str) -> bool:
    """空间里有没有 .xlsx：有才给子 agent 装 xlsx 四件套（省掉纯文档空间的工具 schema）。"""
    try:
        return any(space_dir(space).glob("*.xlsx"))
    except OSError:
        return False


def list_spaces() -> list[str]:
    """知识空间根下所有像空间的目录（本地 demo / 无鉴权上下文时用）。

    只认有 ``AGENT.md`` 或 ``index.md`` 的目录：根下还有装演示数据/散文件的目录
    （result1、secret 之类），它们不是空间。
    """
    try:
        return sorted(
            p.name
            for p in KNOWLEDGE_ROOT.iterdir()
            if p.is_dir() and not p.name.startswith(".") and ((p / AGENT_FILE).exists() or (p / "index.md").exists())
        )
    except OSError:
        return []


def scaffold_space(space: str) -> Path:
    """建空间目录 + ``AGENT.md``（已存在的不动，避免覆盖别人写好的 prompt）。"""
    folder = space_dir(space)
    folder.mkdir(parents=True, exist_ok=True)
    agent_md = folder / AGENT_FILE
    if not agent_md.exists():
        agent_md.write_text(_TEMPLATE.format(space=space), encoding="utf-8")
    return folder


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
