"""虚拟路径 → 本地可读的 xlsx + 索引缓存键（ADR-0015 §4）。

字节由 backend 取（`graphs/tools/xlsx/__init__.py::_fetch`），这里只负责**落缓存**：

- 缓存按**内容 sha16** 命名，同一份文件不管从哪个空间/哪条会话读到都只存一份；
- 索引键同 sha16：文件内容变了 sha 就变，天然换索引，不会答旧表；
- 缓存是**可丢弃**的：删了下次重下。所以每次工具调用都会下载一次——换来的是不会
  读到过期数据（代价见 `shortcut:` 注释）。

`xlsx.cache_dir` 不能落在 OKF 根下——根散文件对所有登录用户只读共享。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

from shared import config


def cache_dir() -> Path:
    return Path(config.get("xlsx.cache_dir")).resolve()


def display_name(path: str | Path) -> str:
    return str(path).rstrip("/\\").rsplit("/", 1)[-1]


def cache_bytes(path: str | Path, data: bytes) -> tuple[Path, str]:
    """返回 (本地 xlsx, 索引缓存键)。同内容并发写：临时名 + os.replace，原子。"""
    sha16 = hashlib.sha256(data).hexdigest()[:16]
    suffix = Path(display_name(path)).suffix or ".xlsx"
    target = cache_dir() / f"{sha16}{suffix}"
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    # shortcut: 每次调用重新取字节（不按 mtime/etag 做缓存命中），所以大表每个工具调用
    # 多一次下载。要省这条就去 backend 加一次 stat/etag 查询，先看实际调用量再说。
    return target, sha16
