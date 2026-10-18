"""图侧的业务上限常量（单一来源，提示词与拦截层都从这里取）。

改值只改环境变量，别在 prompt 或别处再写一份数字——两处各写一份就会出现
"提示词说 3 个、拦截层实际放 4 个"这种漂移。
"""

from __future__ import annotations

import os


def _positive_int(name: str, default: int) -> int:
    """读正整数环境变量；非法值退回默认（启动不因一个错字崩掉）。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


# 一个主管线程同时最多几个**非终态**后台任务（排队中 + 运行中，已完成的释放名额）。
# 超限时拦截层返回错误文本让模型自己等待，见 docs/adr/0008。
MAX_ACTIVE_TASKS = _positive_int("MAX_ACTIVE_TASKS", 3)
