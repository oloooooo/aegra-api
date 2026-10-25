"""一个用户能拥有多少个会话（主管线程）。

放在 `custom/services.py` 旁边而不是 `graphs/`：配额由 HTTP 层执行
（`@auth.on.threads.create`），图运行时不参与。

超额的语义是**拒绝新建**，不是排队：排队的会是一个永远拿不到 thread_id 的请求，
前端无法展示。子 agent 的并发不在这里管——它们在同一条配额链路里（子线程也计数），
而计算开销由 worker 队列（`N_JOBS_PER_WORKER`）自然承接。
"""

from __future__ import annotations

import os


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


# 一个账号最多同时拥有几个会话（主管线程 + 它们的子线程）。
# 超管不受限（见 custom_auth.py）：管理员要能排查别人的问题。
#
# 50 是按“一个会话派 3~5 个子任务”反推的量级：用户实际能同时开的主会话约 10 个。
# 口径仍是“含子线程”（子线程计数到所属主管上），只在建会话时判定。
MAX_THREADS_PER_USER = _positive_int("MAX_THREADS_PER_USER", 50)
