"""虚拟路径 → 本地可读的 xlsx + 索引缓存键（ADR-0015 §4）。

两种来源，读法不同但下游一致（都先落到本地文件，再建/复用 SQLite 索引）：

- `/空间名/x.xlsx`：知识空间里的存量文件，走本地盘（`graphs/<okf.dir>`），只读。
- `/uploads/x.xlsx`：会话文件，字节在对象存储；按 `(上传者, 主管 thread, sha16)` 命名
  落到本机缓存。缓存是**可丢弃**的：删了下次重下。

`xlsx.cache_dir` 不能落在 OKF 根下——根散文件对所有登录用户只读共享。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from shared.session import blobs
from shared import config
from shared.session.store import identity_digest

UPLOADS_PREFIX = "/uploads/"


@dataclass(frozen=True)
class Remote:
    """已从 PG 解析出来的一条会话文件（异步侧查好后传进线程）。"""

    object_key: str
    sha16: str
    name: str
    owner: str
    thread_id: str


def cache_dir() -> Path:
    return Path(config.get("xlsx.cache_dir")).resolve()

def root() -> Path:
    """与 backend 同一根：虚拟路径 /a.xlsx 落到 <okf>/a.xlsx（s3 模式下是本地镜像）。"""
    from shared.knowledge.root import knowledge_root  # 懒导入：避免 tools 包启动时拖上 blobs

    return knowledge_root()


def resolve_local(path: str | Path) -> Path:
    """绝对路径当虚拟路径：/kb/a.xlsx、/a.xlsx 都相对 OKF 根解析，不允许越界。"""
    raw = str(path).strip()
    p = Path(raw)
    if raw.startswith(("/", "\\")):
        root_dir = root()
        p = (root_dir / raw.lstrip("/\\")).resolve()
        if root_dir not in p.parents:
            raise ValueError(f"路径越界：{path}")
    # 相对路径分支（如 knowledge/消融实验1.xlsx）不查越界——那是调用方自己的 cwd
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在：{p}")
    return p


def uploads_name(path: str | Path) -> str | None:
    """`/uploads/x.xlsx` → `x.xlsx`；不是会话文件返回 None。"""
    raw = str(path).strip()
    if not raw.startswith(UPLOADS_PREFIX):
        return None
    name = raw[len(UPLOADS_PREFIX) :]
    if not name or "/" in name or name in {".", ".."}:
        raise ValueError(f"会话文件路径非法：{path}")
    return name


def display_name(path: str | Path) -> str:
    return str(path).rstrip("/\\").rsplit("/", 1)[-1]


def materialize(path: str | Path, remote: Remote | None) -> tuple[Path, str]:
    """返回 (本地 xlsx, 索引缓存键)。"""
    if remote is not None:
        target = cache_dir() / f"{identity_digest(remote.owner)}-{remote.thread_id}-{remote.sha16}.xlsx"
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".xlsx.tmp")
            blobs.download(remote.object_key, str(tmp))
            os.replace(tmp, target)  # 原子：并发下载不会让谁读到半个文件
        return target, f"{identity_digest(remote.owner)}-{remote.thread_id}-{remote.sha16}"

    local = resolve_local(path)
    stat = local.stat()
    # 空间文件可能被手工改过：mtime+size 一起进键，改了就换索引
    return local, f"local-{local.parent.name}-{local.name}-{int(stat.st_mtime)}-{stat.st_size}"
