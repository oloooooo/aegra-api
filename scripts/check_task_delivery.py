"""自检：后台任务结果投递（主管 hook 写 state + notifier 空闲回写）不依赖 LLM。

跑法：``PYTHONPATH=. PYTHONUTF8=1 uv run python scripts/check_task_delivery.py``

覆盖四件事：
1. guard 的 before_agent / after_agent：终态未投递才写、投递过不再写、工具路径已写终态要补投；
2. 判重键是 `(task_id, run_id)`，且 check 过的任务（delivered_run_id）不再重复投；
3. 投递文本与回写共用 `[Async subagent ...]` 前缀 + `task_id:` / `run_id:` 标记，
   摘要超长时带截断标记（前端与模型都靠这些认）；
4. notifier 只在父线程 idle/error 时投一次：busy / interrupted 都不碰。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from langchain_core.messages import HumanMessage
from langgraph.types import Command

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
    guard._parent_thread_id = lambda: "parent"  # type: ignore[method-assign]

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
    # 1. running → success：写一次，且带前缀 / task_id / run_id
    guard, _ = make_guard({"run-t1": "success"}, {"t1": "76011000 = 12.3"})
    state = {"async_tasks": {"t1": task("t1", "running")}, "messages": []}
    out = await guard.abefore_agent(state, None)
    msgs = (out or {}).get("messages") or []
    check("running→success 会写结果", bool(msgs))
    text = msgs[0].content if msgs else ""
    check("投递文本以 [Async subagent 开头（前端认它是子 agent 返回）", text.startswith("[Async subagent 'cbam_reporting'"))
    check("投递文本带 task_id/run_id 标记", "task_id: t1" in text and "run_id: run-t1" in text)
    check("投递文本带结果摘要", "12.3" in text)
    updated = (out or {}).get("async_tasks", {}).get("t1", {})
    check("同时把 state 里的状态刷成 success", updated.get("status") == "success")
    check("并在 task 上记 delivered_run_id", updated.get("delivered_run_id") == "run-t1")

    # 2. 消息已被丢弃、只剩 delivered_run_id：仍不重复投（判重不靠扫消息）
    state2 = {"async_tasks": {"t1": {**task("t1", "success"), "delivered_run_id": "run-t1"}}, "messages": []}
    check("delivered_run_id 已记 → 不再写", await guard.aafter_agent(state2, None) is None)

    # 3. 投递消息在对话里 → 不再写（跨进程/跨 run 的判据）
    guard3, _ = make_guard({}, {})
    delivered = (
        "[Async subagent 'cbam_reporting' has completed] Result: x\n\ntask_id: t3\nrun_id: run-t3"
    )
    state3 = {"async_tasks": {"t3": task("t3", "success")}, "messages": [HumanMessage(content=delivered)]}
    check("消息里已有投递记录 → 不再写", await guard3.aafter_agent(state3, None) is None)

    # 4. 工具路径已把 state 写成终态但没投递 → 补投
    guard4, _ = make_guard({}, {})
    state4 = {"async_tasks": {"t4": task("t4", "success")}, "messages": []}
    check("state 已是终态但未投递 → 补投", bool(((await guard4.aafter_agent(state4, None)) or {}).get("messages")))

    # 5. 还在跑 → 什么都不做
    guard5, _ = make_guard({"run-t5": "running"}, {})
    check("running → 不动", await guard5.abefore_agent({"async_tasks": {"t5": task("t5", "running")}}, None) is None)

    # 6. 摘要超长 → 带截断标记（模型才知道该去 check 取全文）
    guard6, _ = make_guard({"run-t6": "success"}, {"t6": "表" * 900})
    out6 = await guard6.abefore_agent({"async_tasks": {"t6": task("t6", "running")}, "messages": []}, None)
    text6 = (out6 or {})["messages"][0].content
    check("超长摘要带截断标记", "结果已截断" in text6 and "check_async_task" in text6, text6[-60:])

    # 7. 模型自己 check 到终态结果 → 记 delivered_run_id，之后不再重复投
    guard7, _ = make_guard({"run-t7": "success"}, {"t7": "x"})
    checked = Command(update={"messages": [], "async_tasks": {"t7": {**task("t7", "success")}}})
    request = type("Req", (), {"tool_call": {"args": {"task_id": "t7"}}, "state": {"async_tasks": {"t7": task("t7", "running")}}})()
    guard7._mark_checked(request, checked)
    check(
        "check 到终态结果 → 记 delivered_run_id",
        (checked.update["async_tasks"]["t7"]).get("delivered_run_id") == "run-t7",
    )
    check(
        "check 过之后不再补投",
        await guard7.aafter_agent({"async_tasks": checked.update["async_tasks"], "messages": []}, None) is None,
    )

    # 8. 认领失败（另一条路已投）→ 不写投递消息
    guard8, _ = make_guard({"run-t8": "success"}, {"t8": "x"}, claimed=False)
    out8 = await guard8.abefore_agent({"async_tasks": {"t8": task("t8", "running")}, "messages": []}, None)
    check("认领失败 → 不写投递消息", not (out8 or {}).get("messages"), str(out8))
    check(
        "认领失败 → 不记 delivered_run_id",
        not (out8 or {}).get("async_tasks", {}).get("t8", {}).get("delivered_run_id"),
        str(out8),
    )

    # 9. 认领成功但写 state 失败 → 撤销认领（否则结果被永久锁成已投递）
    guard9, _ = make_guard({"run-t9": "success"}, {"t9": "x"})
    releases: list[str] = []

    async def release(task_: dict, parent: str) -> None:
        releases.append(task_["task_id"])

    guard9._release_async = release  # type: ignore[method-assign]

    async def broken_summary(task_: dict) -> str:
        raise RuntimeError("summary failed")

    guard9._summary_async = broken_summary  # type: ignore[method-assign]
    with contextlib.suppress(RuntimeError):
        await guard9.abefore_agent({"async_tasks": {"t9": task("t9", "running")}, "messages": []}, None)
    check("写 state 失败 → 撤销认领", releases == ["t9"], str(releases))


# ---------- 假客户端：notifier 侧 ----------


class FakeThreadView:
    def __init__(self, status: str) -> None:
        self.status = status

    async def get(self, thread_id: str) -> dict:
        return {"thread_id": thread_id, "status": self.status}


class FakeRunCreate:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(self, **kwargs) -> dict:
        self.created.append(kwargs)
        return {"run_id": "new-run"}


class FakeNotifierClient:
    def __init__(self, status: str) -> None:
        self.threads = FakeThreadView(status)
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
    client = FakeNotifierClient(status)
    claims = claims or FakeClaim()
    notifier_mod.get_client = lambda url, headers: client  # type: ignore[assignment]
    notifier_mod._mint_parent_token = lambda identity: "tok"  # type: ignore[assignment]
    notifier_mod._claim_delivery = claims.claim  # type: ignore[assignment]
    notifier_mod._release_delivery = claims.release  # type: ignore[assignment]
    return mw, client, claims


async def notifier_checks() -> None:
    # 1. 父线程 busy → 不投、不认领（结果由主管 hook 负责）
    mw, client, claims = make_notifier("busy")
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t1")
    check("busy → 不投递", client.runs.created == [])
    check("busy → 不认领（让给主管 hook）", claims.keys == set())

    # 2. interrupted（HITL 提问）→ 不碰，绝不用 resume 顶掉用户待答的问题
    mw, client, claims = make_notifier("interrupted")
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t2")
    check("interrupted → 不投递", client.runs.created == [])
    check("interrupted → 不认领", claims.keys == set())

    # 3. 认领失败（另一条路已投）→ 跳过
    claims = FakeClaim()
    claims.keys.add(("parent", "t3", "unknown-run"))
    mw, client, _ = make_notifier("idle", claims)
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t3")
    check("认领失败（已投递/在投）→ 跳过", client.runs.created == [])

    # 4. idle 且认领成功 → 开新一轮投递
    mw, client, claims = make_notifier("idle")
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t4")
    check("idle 且抢到认领 → 投递一次", len(client.runs.created) == 1, str(client.runs.created))
    sent = (client.runs.created[0].get("input") or {}).get("messages", [{}])[0].get("content", "")
    check("投递消息带 task_id / run_id 标记", "task_id: t4" in sent and "run_id: unknown-run" in sent)

    # 5. 两个不同任务并发完成 → 各投一次
    shared = FakeClaim()
    mw_a, client_a, _ = make_notifier("idle", shared)
    mw_b, _, _ = make_notifier("idle", shared)
    notifier_mod.get_client = lambda url, headers: client_a  # type: ignore[assignment]
    await asyncio.gather(
        mw_a._send_notification("[Async subagent 'cbam_reporting' has completed] Result: A", "t6a"),
        mw_b._send_notification("[Async subagent 'hybrid_bonding' has completed] Result: B", "t6b"),
    )
    check("不同任务并发 → 各投递一次", len(client_a.runs.created) == 2, str(len(client_a.runs.created)))

    # 6. 同一任务并发投递两次 → 只投一次
    shared = FakeClaim()
    mw_c, client_c, _ = make_notifier("idle", shared)
    mw_d, _, _ = make_notifier("idle", shared)
    notifier_mod.get_client = lambda url, headers: client_c  # type: ignore[assignment]
    await asyncio.gather(
        mw_c._send_notification("[Async subagent 'cbam_reporting' has completed] Result: A", "t7"),
        mw_d._send_notification("[Async subagent 'cbam_reporting' has completed] Result: A", "t7"),
    )
    check("同一任务并发 → 只投一次", len(client_c.runs.created) == 1, str(len(client_c.runs.created)))

    # 7. update_async_task 后的新 run → 新结果再投一次（判重键含 run_id）
    shared = FakeClaim()
    mw_e, client_e, _ = make_notifier("idle", shared, run_key="run-old")
    mw_f, _, _ = make_notifier("idle", shared, run_key="run-new")
    notifier_mod.get_client = lambda url, headers: client_e  # type: ignore[assignment]
    await mw_e._send_notification("[Async subagent 'cbam_reporting' has completed] Result: 旧", "t8")
    await mw_f._send_notification("[Async subagent 'cbam_reporting' has completed] Result: 新", "t8")
    check("同一任务的新 run → 再投一次", len(client_e.runs.created) == 2, str(len(client_e.runs.created)))

    # 8. 开 run 失败 → 撤销认领且只试一次（否则结果被永久判为已投递）
    mw, client, claims = make_notifier("idle")
    client.runs.create = _boom  # type: ignore[method-assign]
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t9")
    check(
        "投递失败 → 只试一次，撤销认领",
        len(claims.releases) == 1 and claims.keys == set(),
        f"{claims.releases!r} keys={claims.keys!r}",
    )

    # 9. 未知线程状态 → 不碰（交给主管 hook 兜底）
    mw, client, claims = make_notifier("unknown")
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t5")
    check("未知状态 → 不投递", client.runs.created == [])

    # 10. 子 agent 的摘要超长同样带截断标记
    long_summary = notifier_mod._extract_last_message({"messages": [HumanMessage(content="表" * 900)]})
    check("子 agent 摘要超长带截断标记", "结果已截断" in long_summary, long_summary[-40:])


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
