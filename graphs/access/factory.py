"""一次 run 的存储路由（唯一装配点）。

图和读文件的工具都从这里拿 backend:同一个装配点,`/uploads/` 走哪条路由只在一处决定。
"""

from deepagents.backends import CompositeBackend, StoreBackend

from graphs.access import session_scope
from graphs.access.backend import SpaceBackend
from graphs.access.s3_backend import UploadsBackend
from shared.knowledge.root import knowledge_root
from shared.session import blobs


def session_backend() -> CompositeBackend:
    """默认按 space 隔离，`/uploads/` 读本会话上传的文件。

    包在外面而不是改 `SpaceBackend`：后者是「这次 run 能读哪些 space」的判定单点（ADR-0005），
    路由是另一件事；`CompositeBackend` 是纯前缀匹配，`/uploads/x.md` 根本不会到它那里。
    上传字节在对象存储时按台账读（`UploadsBackend`）；没配对象存储（本地 demo）退回
    会话的 LangGraph Store 命名空间。
    """
    default = SpaceBackend(root_dir=knowledge_root())
    if not blobs.configured():
        return CompositeBackend(default=default, routes={"/uploads/": StoreBackend(namespace=session_scope.uploads_namespace)})
    return CompositeBackend(default=default, routes={"/uploads/": UploadsBackend()})
