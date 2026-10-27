"""一次 run 的存储路由（唯一装配点）。

图和读文件的工具都从这里拿 backend:同一个装配点,`/uploads/` 走哪条路由只在一处决定。
"""

from deepagents.backends import CompositeBackend

from graphs.access.backend import SpaceBackend
from graphs.access.s3_backend import S3Backend, UploadsBackend
from shared.knowledge.root import backend_mode, knowledge_root


def session_backend() -> CompositeBackend:
    """默认按 space 隔离，`/uploads/` 读本会话上传的文件。

    包在外面而不是改 `SpaceBackend`：后者是「这次 run 能读哪些 space」的判定单点（ADR-0005），
    路由是另一件事；`CompositeBackend` 是纯前缀匹配，`/uploads/x.md` 根本不会到它那里。

    默认路由两种：`okf.backend: s3fs` 时空间内容直读对象存储（`S3Backend`，无需本地副本）；
    其余模式是本地目录（`SpaceBackend`，`s3` 模式读的就是镜像下来的副本）。两条路共用同
    一套空间判定（`SpaceGuard`），所以「这次 run 能读哪些 space」在两种模式下等价。

    上传的文件（md 与 xlsx）字节都在对象存储、key 记在 PG 台账，所以 `/uploads/` 恒为
    `UploadsBackend`；没配对象存储就没有上传，这个路由自然读不到东西。
    """
    default = S3Backend() if backend_mode() == "s3fs" else SpaceBackend(root_dir=knowledge_root())
    return CompositeBackend(default=default, routes={"/uploads/": UploadsBackend()})
