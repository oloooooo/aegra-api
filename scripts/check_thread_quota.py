"""自检：会话配额与级联删子线程（ADR-0012），不依赖数据库与 LLM。

跑法：``PYTHONPATH=. PYTHONUTF8=1 uv run python scripts/check_thread_quota.py``

覆盖五件事：
1. `@auth.on.threads.create`：配额内放行并把 owner_identity 写进 metadata，超额 403，超管豁免；
2. 客户端自带的 metadata 不被覆盖；
3. `@auth.on.threads.delete`：别人的线程不动、自己的线程级联删、超管删别人的也可以；
4. **顺序**：先删 checkpoint 再删 thread 行（反序会留孤儿 checkpoint）；
5. 不带 thread_id 的批量路径（prune）不触发级联。
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import custom.middleware.auth as auth_mod
from custom.service.quota import MAX_THREADS_PER_USER

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'✓' if ok else '✗'} {name}{'' if ok else f'  ← {detail}'}")
    if not ok:
        FAILS.append(name)


class FakeSession:
    """最小 AsyncSession：只回答 scalar，并记录删除动作。"""

    def __init__(self, *, owner: str | None = None, used: int = 0, children: list[str] | None = None):
        self.owner = owner
        self.used = used
        self.children = children or []

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def scalar(self, stmt: object) -> object:
        # 归属查询返回行属主；配额查询走仓库替身，不经过这里
        return self.owner


def install(session_factory, calls: list[str]) -> None:
    """把 auth 模块的 DB/仓库/checkpointer 全部换成记录型的替身。"""

    class Repo:
        def __init__(self, session: FakeSession) -> None:
            self.session = session

        async def list_children(self, parent_thread_id: str) -> list[str]:
            return list(self.session.children)

        async def delete_threads(self, thread_ids: list[str]) -> None:
            calls.append(f"rows:{','.join(thread_ids)}")

        async def count_owned(self, identity: str) -> int:
            return self.session.used

    class Checkpointer:
        async def adelete_thread(self, thread_id: str) -> None:
            calls.append(f"checkpoint:{thread_id}")

    auth_mod.get_session_maker = lambda: session_factory  # pyright: ignore[reportAttributeAccessIssue]
    auth_mod.ThreadQuotaRepository = Repo  # pyright: ignore[reportAttributeAccessIssue]
    auth_mod.db_manager = SimpleNamespace(get_checkpointer=lambda: Checkpointer())  # pyright: ignore[reportAttributeAccessIssue]


def ctx(identity: str, perms: list[str]) -> SimpleNamespace:
    return SimpleNamespace(user=SimpleNamespace(identity=identity), permissions=perms)


def session_factory(**kwargs):  # noqa: ANN201 — 返回可调用的 session maker
    return lambda: FakeSession(**kwargs)


async def create_checks() -> None:
    # 1. 配额内：放行 + 写入 owner_identity
    install(session_factory(used=MAX_THREADS_PER_USER - 1), [])
    value: dict = {"metadata": None}
    await auth_mod._quota_threads_create(ctx("alice", ["read"]), value)
    check("配额内放行", True)
    check("写入 owner_identity", (value.get("metadata") or {}).get("owner_identity") == "alice", value)

    # 2. 客户端自带 metadata 不被覆盖
    value = {"metadata": {"thread_name": "x"}}
    await auth_mod._quota_threads_create(ctx("alice", ["read"]), value)
    check("保留客户端 metadata", value["metadata"] == {"thread_name": "x", "owner_identity": "alice"}, value)

    # 3. 超额 403
    install(session_factory(used=MAX_THREADS_PER_USER), [])
    try:
        await auth_mod._quota_threads_create(ctx("alice", ["read"]), {"metadata": None})
        check("超额被拒", False, "没有抛出")
    except Exception as e:  # noqa: BLE001 — auth 异常类型由 SDK 提供
        check("超额被拒 403", getattr(e, "status_code", None) == 403, getattr(e, "status_code", "?"))
        check("文案可读", "会话数已达上限" in str(e), str(e))

    # 4. 超管豁免
    install(session_factory(used=10_000), [])
    await auth_mod._quota_threads_create(ctx("root", ["read", "write"]), {"metadata": None})
    check("超管不受限", True)


async def cascade_checks() -> None:
    # 1. 自己的线程：先 checkpoint 后删行
    calls: list[str] = []
    install(session_factory(owner="alice", children=["c1", "c2"]), calls)
    await auth_mod._cascade_child_threads(ctx("alice", ["read"]), {"thread_id": "p1"})
    check("级联删了子线程", "rows:c1,c2" in calls, calls)
    check(
        "顺序：先删 checkpoint 再删行",
        calls == ["checkpoint:c1", "checkpoint:c2", "rows:c1,c2"],
        calls,
    )

    # 2. 别人的线程：什么都不动
    calls = []
    install(session_factory(owner="bob", children=["c9"]), calls)
    await auth_mod._cascade_child_threads(ctx("alice", ["read"]), {"thread_id": "p2"})
    check("别人的线程不动", calls == [], calls)

    # 3. 超管删别人的：允许
    calls = []
    install(session_factory(owner="bob", children=["c9"]), calls)
    await auth_mod._cascade_child_threads(ctx("root", ["read", "write"]), {"thread_id": "p2"})
    check("超管可删别人的", calls == ["checkpoint:c9", "rows:c9"], calls)

    # 4. 批量路径（prune）不带 thread_id：不触发
    calls = []
    install(session_factory(owner="alice", children=["c1"]), calls)
    await auth_mod._cascade_child_threads(ctx("alice", ["read"]), {})
    check("prune 路径不触发级联", calls == [], calls)

    # 5. 线程不存在（None）：不触发
    calls = []
    install(session_factory(owner=None, children=["c1"]), calls)
    await auth_mod._cascade_child_threads(ctx("alice", ["read"]), {"thread_id": "missing"})
    check("线程不存在不触发级联", calls == [], calls)


async def main() -> None:
    await create_checks()
    await cascade_checks()
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        sys.exit(1)
    print("全部通过 ✔")


if __name__ == "__main__":
    asyncio.run(main())
