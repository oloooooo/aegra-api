"""知识空间根（ADR-0016 / 本分支 ADR-0018）。

本分支只跑「本地 filesystem + Store」：空间内容就是仓库根的 `knowledge/`，没有对象存储、
没有本地镜像。想让另一套部署读对象存储，走 `feat/unified-files-s3` 分支——两条分支共用
同一套空间判定与读文件入口（`graphs/access/`），差别只在字节从哪来。
"""

from __future__ import annotations

from pathlib import Path

from shared import config

PROJECT_ROOT = config.ROOT


def local_root() -> Path:
    """仓库里自带的知识空间根。

    数据不在代码包里：`knowledge/` 是仓库根的顶层目录，不是 `graphs/` 的子目录。
    """
    return (PROJECT_ROOT / config.get("okf.dir")).resolve()


def knowledge_root() -> Path:
    """当前生效的知识空间根（只有一个来源：本地盘）。"""
    return local_root()
