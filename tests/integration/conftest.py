"""Integration test specific fixtures

Integration tests may use database and multiple components together.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest


class _EmptyResult:
    """`session.execute()` 的最小返回值：只回答 custom_auth 用到的两种取法。"""

    def scalar_one(self) -> int:
        return 0  # 会话配额 COUNT(*)：不触顶

    def scalars(self) -> list[str]:
        return []  # 子线程列表：空 → 级联删除直接跳过


class _StubSession:
    async def execute(self, *args: Any, **kwargs: Any) -> _EmptyResult:
        return _EmptyResult()

    async def scalar(self, *args: Any, **kwargs: Any) -> None:
        # 线程属主查询：None → 级联删除处理器不动任何东西，交给原生接口判 404/403
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@asynccontextmanager
async def _stub_session_cm() -> AsyncIterator[_StubSession]:
    yield _StubSession()


@pytest.fixture(autouse=True)
def stub_session_maker(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """给 `custom/custom_auth.py` 的 `@auth.on` 处理器塞一个假 session maker。

    那几条处理器（会话配额、级联删子线程）**直接用 `get_session_maker()`**，不走
    FastAPI 的 `get_session` 依赖，所以用例里 `override_get_session_dep` 覆盖不到
    它们；而 `db_manager` 在测试里从不 initialize，于是一碰 `/threads`、`/runs` 就
    `RuntimeError: Database not initialized`（此前 integration 的 41 个用例全栽在这）。

    补丁打在 `aegra_api.core.orm.async_session_maker` 这个**模块级缓存**上，而不是
    `custom.custom_auth.get_session_maker`：自定义 app/auth 是 `importlib` 按
    `custom_app_module` 之类的合成名加载的，`custom.custom_auth` 与它可能是两个模块
    实例，patch 前者对后者无效。缓存只有一个，`get_session_maker()` 谁调都先看它。

    只回答处理器真正要的两类查询：配额计数（恒 0）和线程属主（恒 None）。业务断言仍
    靠各用例自己的 session mock，不引入真实数据库依赖。
    """
    monkeypatch.setattr("aegra_api.core.orm.async_session_maker", _stub_session_cm)
    yield
