"""自检：后台任务结果投递（① 注入 + notifier 四态分派）不依赖 LLM。

跑法：``PYTHONPATH=. PYTHONUTF8=1 uv run python scripts/check_task_delivery.py``

覆盖三件事：
1. guard 的 before_model：终态未投递才注入、投递过不再注入、工具路径已写终态要补投；
2. 注入文本与回写共用 `[Async subagent ...]` 前缀 + `task_id:` 标记（前端才认得出）；
3. notifier 的四种父线程状态：busy 不插队、interrupted 不碰（HITL）、已投递跳过、idle 才投递。
"""

from __future__ import annotations

import asyncio
import sys

from langchain_core.messages import HumanMessage

from custom.middlewares import completion_notifier as notifier_mod
from graphs.async_task_guard import AsyncTaskGuard

FAILS: list[str] = []


async def _boom(**kwargs: object) -> dict:
    raise RuntimeError("create failed")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'✓' if ok else '✗'} {name}{'' if ok else f'  ← {detail}'}")
    if not ok:
        FAILS.append(name)


# ---------- 假客户端：guard 侧 ----------


class FakeRuns:
    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses
        self.calls = 0

    async def get(self, thread_id: str, run_id: str) -> dict:
        self.calls += 1
        return {"status": self.statuses.get(run_id, "running")}


class FakeThreads:
    def __init__(self, texts: dict[str, str]) -> None:
        self.texts = texts

    async def get(self, thread_id: str) -> dict:
        return {"values": {"messages": [{"type": "ai", "content": self.texts.get(thread_id, "")}]}}


class FakeClient:
    def __init__(self, statuses: dict[str, str], texts: dict[str, str]) -> None:
        self.runs = FakeRuns(statuses)
        self.threads = FakeThreads(texts)


def make_guard(
    statuses: dict[str, str], texts: dict[str, str], claimed: bool = True
) -> tuple[AsyncTaskGuard, FakeClient]:
    guard = AsyncTaskGuard({"cbam_reporting": {"url": "http://fake", "graph_id": "cbam_reporting"}})
    client = FakeClient(statuses, texts)
    guard._async_client = lambda spec: client  # type: ignore[method-assign]

    async def claim(task: dict, parent: str) -> bool:
        return claimed

    guard._claim_async = claim  # type: ignore[method-assign]
    return guard, client


def task(task_id: str, status: str) -> dict:
    return {
        "task_id": task_id,
        "agent_name": "cbam_reporting",
        "thread_id": task_id,
        "run_id": f"run-{task_id}",
        "status": status,
    }


async def guard_checks() -> None:
    # 1. running → success：注入一次，且带 task_id 标记与前缀
    guard, _ = make_guard({"run-t1": "success"}, {"t1": "76011000 = 12.3"})
    state = {"async_tasks": {"t1": task("t1", "running")}, "messages": []}
    out = await guard.abefore_model(state, None)
    msgs = (out or {}).get("messages") or []
    check("running→success 会注入", bool(msgs))
    text = msgs[0].content if msgs else ""
    check("注入文本以 [Async subagent 开头（前端认它是子 agent 返回）", text.startswith("[Async subagent 'cbam_reporting'"))
    check("注入文本带 task_id/run_id 标记", "task_id: t1" in text and "run_id: run-t1" in text)
    check("注入文本带结果摘要", "12.3" in text)
    check("同时把 state 里的状态刷成 success", (out or {}).get("async_tasks", {}).get("t1", {}).get("status") == "success")

    # 2. 再把注入后的 state 喂回去：不应重复注入
    state2 = {
        "async_tasks": {(out or {})["async_tasks"].get("t1", state["async_tasks"]["t1"])["task_id"]: task("t1", "success")},
        "messages": [HumanMessage(content=text)],
    }
    check("已投递过 → 不再注入", await guard.abefore_model(state2, None) is None)

    # 3. 工具路径已把 state 写成终态但没投递 → 补投
    guard3, _ = make_guard({}, {})
    state3 = {"async_tasks": {"t3": task("t3", "success")}, "messages": []}
    out3 = await guard3.abefore_model(state3, None)
    check("state 已是终态但未投递 → 补投", bool((out3 or {}).get("messages")))

    # 4. 还在跑 → 什么都不做
    guard4, _ = make_guard({"run-t4": "running"}, {})
    check("running → 不动", await guard4.abefore_model({"async_tasks": {"t4": task("t4", "running")}}, None) is None)

    # 5. TTL 缓存：同一任务连续两次 before_model 只查一次服务端
    guard5, client5 = make_guard({"run-t5": "running"}, {})
    state5 = {"async_tasks": {"t5": task("t5", "running")}, "messages": []}
    await guard5.abefore_model(state5, None)
    await guard5.abefore_model(state5, None)
    check("TTL 缓存生效（两次检查只查 1 次）", client5.runs.calls == 1, f"calls={client5.runs.calls}")


# ---------- 假客户端：notifier 侧 ----------


class FakeThreadView:
    def __init__(self, status: str, snapshot: dict) -> None:
        self.status = status
        self.snapshot = snapshot

    async def get(self, thread_id: str) -> dict:
        return {"thread_id": thread_id, "status": self.status}

    async def get_state(self, thread_id: str) -> dict:
        return self.snapshot


class FakeRunCreate:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(self, **kwargs) -> dict:
        self.created.append(kwargs)
        return {"run_id": "new-run"}


class FakeNotifierClient:
    def __init__(self, status: str, snapshot: dict) -> None:
        self.threads = FakeThreadView(status, snapshot)
        self.runs = FakeRunCreate()


class FakeClaim:
    """模拟服务端认领表：主键 (parent, task, run)，先到先得。"""

    def __init__(self) -> None:
        self.keys: set[tuple[str, str, str]] = set()
        self.releases: list[tuple[str, str, str]] = []

    async def claim(self, url: str, token: str, parent: str, task: str, run: str) -> tuple[bool, str]:
        key = (parent, task, run)
        if key in self.keys:
            return False, "delivered"
        self.keys.add(key)
        return True, "claimed"

    async def release(self, url: str, token: str, parent: str, task: str, run: str) -> None:
        self.releases.append((parent, task, run))
        self.keys.discard((parent, task, run))


def make_notifier(
    status: str,
    snapshot: dict,
    claims: FakeClaim | None = None,
    run_key: str | None = None,
) -> tuple[notifier_mod.CompletionNotifierMiddleware, FakeNotifierClient, FakeClaim]:
    mw = notifier_mod.CompletionNotifierMiddleware(subagent_name="cbam_reporting")
    mw.parent_thread_id = "parent"
    mw.parent_assistant_id = "supervisor"
    mw.parent_user_identity = "root"
    mw.notify_url = "http://fake"
    if run_key is not None:
        mw._run_key = lambda: run_key  # type: ignore[method-assign]
    client = FakeNotifierClient(status, snapshot)
    claims = claims or FakeClaim()
    notifier_mod.get_client = lambda url, headers: client  # type: ignore[assignment]
    notifier_mod._mint_parent_token = lambda identity: "tok"  # type: ignore[assignment]
    notifier_mod._claim_delivery = claims.claim  # type: ignore[assignment]
    notifier_mod._release_delivery = claims.release  # type: ignore[assignment]
    return mw, client, claims


async def notifier_checks() -> None:
    # 1. 父线程 busy → 不插队
    mw, client, claims = make_notifier("busy", {})
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t1")
    check("busy → 不投递", client.runs.created == [])
    check("busy → 不认领（让给主管侧注入）", claims.keys == set())
    check("busy → 安排了延迟复查", len(notifier_mod._RECHECK_TASKS) == 1)
    for t in list(notifier_mod._RECHECK_TASKS):
        t.cancel()
    await asyncio.sleep(0)
    notifier_mod._RECHECK_TASKS.clear()

    # 2. interrupted（HITL 提问）→ 不碰
    mw, client, claims = make_notifier("interrupted", {"interrupts": [{"value": {"tool": "ask_user_question"}}]})
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t2")
    check("interrupted(ask) → 不 resume 也不投递", client.runs.created == [])
    check("interrupted(ask) → 没有安排复查", len(notifier_mod._RECHECK_TASKS) == 0)

    # 3. 认领失败（别的路径已投递）→ 跳过
    claims = FakeClaim()
    claims.keys.add(("parent", "t3", "unknown-run"))
    mw, client, _ = make_notifier("idle", {"values": {"messages": []}}, claims)
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t3")
    check("认领失败（已投递/在投）→ 跳过", client.runs.created == [])

    # 4. idle 且认领成功 → 开新一轮投递
    mw, client, claims = make_notifier("idle", {"values": {"messages": []}})
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t4")
    check("idle 且抢到认领 → 投递一次", len(client.runs.created) == 1, str(client.runs.created))
    sent = (client.runs.created[0].get("input") or {}).get("messages", [{}])[0].get("content", "")
    check("投递消息带 task_id / run_id 标记", "task_id: t4" in sent and "run_id: unknown-run" in sent)

    # 5. 两个不同任务并发完成 → 各投一次
    shared = FakeClaim()
    mw_a, client_a, _ = make_notifier("idle", {"values": {"messages": []}}, shared)
    mw_b, _, _ = make_notifier("idle", {"values": {"messages": []}}, shared)
    notifier_mod.get_client = lambda url, headers: client_a  # type: ignore[assignment]
    await asyncio.gather(
        mw_a._send_notification("[Async subagent 'cbam_reporting' has completed] Result: A", "t6a"),
        mw_b._send_notification("[Async subagent 'hybrid_bonding' has completed] Result: B", "t6b"),
    )
    check("不同任务并发 → 各投递一次", len(client_a.runs.created) == 2, str(len(client_a.runs.created)))

    # 6. 同一任务并发投递两次 → 只投一次
    shared = FakeClaim()
    mw_c, client_c, _ = make_notifier("idle", {"values": {"messages": []}}, shared)
    mw_d, _, _ = make_notifier("idle", {"values": {"messages": []}}, shared)
    notifier_mod.get_client = lambda url, headers: client_c  # type: ignore[assignment]
    await asyncio.gather(
        mw_c._send_notification("[Async subagent 'cbam_reporting' has completed] Result: A", "t7"),
        mw_d._send_notification("[Async subagent 'cbam_reporting' has completed] Result: A", "t7"),
    )
    check("同一任务并发 → 只投一次", len(client_c.runs.created) == 1, str(len(client_c.runs.created)))

    # 7. update_async_task 后的新 run → 新结果再投一次（判重键含 run_id）
    shared = FakeClaim()
    mw_e, client_e, _ = make_notifier("idle", {"values": {"messages": []}}, shared, run_key="run-old")
    mw_f, _, _ = make_notifier("idle", {"values": {"messages": []}}, shared, run_key="run-new")
    notifier_mod.get_client = lambda url, headers: client_e  # type: ignore[assignment]
    await mw_e._send_notification("[Async subagent 'cbam_reporting' has completed] Result: 旧", "t8")
    await mw_f._send_notification("[Async subagent 'cbam_reporting' has completed] Result: 新", "t8")
    check("同一任务的新 run → 再投一次", len(client_e.runs.created) == 2, str(len(client_e.runs.created)))

    # 8. 开 run 失败 → 撤销认领（否则这次结果会被永久判为已投递）
    mw, client, claims = make_notifier("idle", {"values": {"messages": []}})
    client.runs.create = _boom  # type: ignore[method-assign]
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t9")
    check(
        "投递失败（重试 3 次）→ 每次都撤销认领，最终认领为空",
        len(claims.releases) == 3 and claims.keys == set(),
        f"{claims.releases!r} keys={claims.keys!r}",
    )

    # 9. 未知中断 → 不碰
    mw, client, claims = make_notifier("interrupted", {"interrupts": [{"value": {"kind": "some_future_thing"}}]})
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t5")
    check("未知中断 → 不碰", client.runs.created == [])


async def main() -> None:
    await guard_checks()
    await notifier_checks()
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        sys.exit(1)
    print("全部通过 ✔")


if __name__ == "__main__":
    asyncio.run(main())
