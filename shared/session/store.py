"""会话文件在 LangGraph Store 里的遗留清场（ADR-0013，已被 ADR-0017 取代）。

md 的字节已随 ADR-0017 搬到对象存储（`uploads/<uuid>/<名>` + `session_object` 台账），
所以这里只剩一件事：删会话时把**旧部署遗留在 Store 命名空间里**的文件清掉——老数据不会
自己消失，而它们已经没有读者了。

两条不能改的约定（历史文件的 key 已按它们写死）：

- **key 带前导斜杠**。当年 `CompositeBackend` 把 `/uploads/x.md` 剥成 `/x.md` 直接交给
  `StoreBackend`，后者拿这个字符串当 store key。
- **命名空间里的身份做 hash**。`StoreBackend._validate_namespace` 只收
  `[A-Za-z0-9\\-_.@+:~]`，而 `user_tables.identity` 允许中文。
"""

from __future__ import annotations

import hashlib
from typing import Any

# 只是 store 的 batch op 类型（轻量模块），不是整张图
from langgraph.store.base import PutOp

# 命名空间前缀。第二期的空间表用别的前缀（如 "space_tables"），别混。
NAMESPACE_PREFIX = "uploads"


def identity_digest(identity: str) -> str:
    """身份的短摘要（历史命名空间用它，现场已不再写入）。"""
    return hashlib.sha256((identity or "").encode("utf-8")).hexdigest()[:16]


def session_namespace(identity: str, thread_id: str) -> tuple[str, ...]:
    """历史会话文件的 store 命名空间：(uploads, h(身份), 主管 thread_id)。"""
    return (NAMESPACE_PREFIX, identity_digest(identity), thread_id)


# 单次会话清理最多跑几轮（500 × 20 = 1 万个文件）；防 store 行为异常时无限循环
_MAX_CLEAR_ROUNDS = 20


async def clear(store: Any, identity: str, thread_id: str) -> int:
    """删光这个会话遗留的文件，返回删掉的条数（删会话时调用，幂等）。

    循环到取不出为止：`asearch` 自带 limit，单次删不完就会静默留下文件
    （只能等孤儿保留期搮底）。
    """
    ns = session_namespace(identity, thread_id)
    removed = 0
    for _ in range(_MAX_CLEAR_ROUNDS):
        items = await store.asearch(ns, limit=500)
        if not items:
            return removed
        # 没有 DeleteOp：store 的删除就是 PutOp(value=None)
        await store.abatch([PutOp(ns, str(item.key), None) for item in items])
        removed += len(items)
    # 剩下的留给孤儿保留期（ADR-0013），总比无限循环好
    return removed
