"""一次 run 的存储路由（唯一装配点）。

图和读文件的工具都从这里拿 backend：默认按 space 隔离、`/uploads/` 读本会话上传的文件。
路由只在这一处决定，`space_agent` 与 `supervisor` 不再各自拼一个 `CompositeBackend`。
"""

from deepagents.backends import CompositeBackend

from graphs.access.backend import SpaceBackend
from graphs.access.uploads_backend import UploadsBackend
from shared.knowledge.root import knowledge_root


def session_backend() -> CompositeBackend:
    """默认按 space 隔离，`/uploads/` 读本会话上传的文件。

    包在外面而不是改 `SpaceBackend`：后者是「这次 run 能读哪些 space」的判定单点
    （ADR-0005），路由是另一件事；`CompositeBackend` 是纯前缀匹配，`/uploads/x.md`
    根本不会到它那里。
    """
    return CompositeBackend(
        default=SpaceBackend(root_dir=knowledge_root()),
        routes={"/uploads/": UploadsBackend()},
    )
