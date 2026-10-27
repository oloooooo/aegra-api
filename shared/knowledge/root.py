"""知识空间根：`okf.backend: local` 读仓库根目录，`s3fs` 直读对象存储（ADR-0017）。

**两种模式**（`config.yaml` 的 `okf.backend`）：

- ``local``（默认，本地开发/测试）：直接读仓库根的 ``knowledge/``。
- ``s3fs``：空间内容直接在对象存储的 ``okf/`` 前缀下，读由 ``graphs/access/s3_backend.py``
  的 ``S3Backend`` 完成，**本进程不落任何本地副本**：多副本看到同一份、改了立刻生效。

旧的 ``s3`` 镜像模式（整目录下拉到 ``.cache/okf`` + TTL 刷新）已退役（ADR-0017）：
它换来的是最终一致 + 一个必须手工执行的 ``push``，而自研 ``S3Backend`` 已经能把
「ls 的 virtual_mode 语义 / grep 的 glob 契约 / read 的分页与二进制判定」做对——
那些细语义都在 `deepagents.backends.utils` 里，不用重写。
"""

from __future__ import annotations

from pathlib import Path

from shared import config

PROJECT_ROOT = config.ROOT


def local_root() -> Path:
    """仓库里自带的知识空间根（`local` 模式的真相）。

    数据不在代码包里：`knowledge/` 是仓库根的顶层目录，不是 `graphs/` 的子目录。
    """
    return (PROJECT_ROOT / config.get("okf.dir")).resolve()


def backend_mode() -> str:
    return str(config.get("okf.backend")).strip().lower()


def knowledge_root() -> Path:
    """当前生效的本地知识空间根（只有 `local` 模式有）。

    ``s3fs`` 模式没有本地根：调用到这里就是有代码没改完（空间内容该经 ``S3Backend`` 读，
    或者用 `shared.knowledge.spec` 里带模式分支的函数），直接报错比静默降级到某个本地目录强。
    """
    mode = backend_mode()
    if mode != "local":
        raise RuntimeError(
            f"okf.backend={mode} 没有本地知识空间根：空间内容在对象存储 okf/ 前缀下，"
            "读走 graphs/access/s3_backend.py（或 shared.knowledge.spec 里的模式分支函数）"
        )
    return local_root()
