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


def make_guard(statuses: dict[str, str], texts: dict[str, str]) -> tuple[AsyncTaskGuard, FakeClient]:
    guard = AsyncTaskGuard({"cbam_reporting": {"url": "http://fake", "graph_id": "cbam_reporting"}})
    client = FakeClient(statuses, texts)
    guard._async_client = lambda spec: client  # type: ignore[method-assign]
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
    check("注入文本带 task_id 标记", "task_id: t1" in text)
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


def make_notifier(status: str, snapshot: dict) -> tuple[notifier_mod.CompletionNotifierMiddleware, FakeNotifierClient]:
    mw = notifier_mod.CompletionNotifierMiddleware(subagent_name="cbam_reporting")
    mw.parent_thread_id = "parent"
    mw.parent_assistant_id = "supervisor"
    mw.parent_user_identity = "root"
    mw.notify_url = "http://fake"
    client = FakeNotifierClient(status, snapshot)
    notifier_mod.get_client = lambda url, headers: client  # type: ignore[assignment]
    notifier_mod._mint_parent_token = lambda identity: "tok"  # type: ignore[assignment]
    return mw, client


async def notifier_checks() -> None:
    # 1. 父线程 busy → 不插队
    mw, client = make_notifier("busy", {})
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t1")
    check("busy → 不投递", client.runs.created == [])
    check("busy → 安排了延迟复查", len(notifier_mod._RECHECK_TASKS) == 1)
    for t in list(notifier_mod._RECHECK_TASKS):
        t.cancel()
    await asyncio.sleep(0)  # 让 done_callback 把已取消的任务从集合里摘掉
    notifier_mod._RECHECK_TASKS.clear()

    # 2. interrupted（HITL 提问）→ 不碰
    mw, client = make_notifier("interrupted", {"interrupts": [{"value": {"tool": "ask_user_question"}}]})
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t2")
    check("interrupted(ask) → 不 resume 也不投递", client.runs.created == [])
    check("interrupted(ask) → 没有安排复查", len(notifier_mod._RECHECK_TASKS) == 0)

    # 3. idle 且 state 里已有投递记录 → 跳过
    delivered = "[Async subagent 'cbam_reporting' has completed] Result: 结果" + chr(10) * 2
    delivered += "task_id: t3"
    snapshot = {"values": {"messages": [{"type": "human", "content": delivered}]}}
    mw, client = make_notifier("idle", snapshot)
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t3")
    check("已投递过 → 跳过", client.runs.created == [])

    # 4. idle 且没有投递记录 → 开新一轮投递
    mw, client = make_notifier("idle", {"values": {"messages": []}})
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t4")
    check("idle 且未投递 → 投递一次", len(client.runs.created) == 1, str(client.runs.created))
    sent = (client.runs.created[0].get("input") or {}).get("messages", [{}])[0].get("content", "")
    check("投递消息带 task_id 标记", "task_id: t4" in sent)

    # 4b. 派活工具消息里的 task_id 不算"已投递"（回归：曾因此永不投递）
    notifier_mod._DELIVERED_LOCAL.clear()
    notifier_mod._DELIVERED_ORDER.clear()
    launch = {"values": {"messages": [{"type": "tool", "content": "Launched async subagent. task_id: t4"}]}}
    mw, client = make_notifier("idle", launch)
    await mw._send_notification("[Async subagent 'cbam_reporting' has completed] Result: x", "t4")
    check("派活消息不算已投递 → 仍会投递", len(client.runs.created) == 1, str(client.runs.created))

    # 4c. 两个子 agent 同时完成（父线程 idle）：只允许开出 1 个投递 run
    notifier_mod._DELIVERED_LOCAL.clear()
    notifier_mod._DELIVERED_ORDER.clear()
    mw_a, client_a = make_notifier("idle", {"values": {"messages": []}})
    # 两个中间件共用同一个假客户端，模拟同一父线程上的两次并发投递
    mw_b = notifier_mod.CompletionNotifierMiddleware(subagent_name="hybrid_bonding")
    for attr in ("parent_thread_id", "parent_assistant_id", "parent_user_identity", "notify_url"):
        setattr(mw_b, attr, getattr(mw_a, attr))
    notifier_mod.get_client = lambda url, headers: client_a  # type: ignore[assignment]
    await asyncio.gather(
        mw_a._send_notification("[Async subagent 'cbam_reporting' has completed] Result: A", "t6a"),
        mw_b._send_notification("[Async subagent 'hybrid_bonding' has completed] Result: B", "t6b"),
    )
    check("并发完成：两个不同任务各投递一次", len(client_a.runs.created) == 2, str(len(client_a.runs.created)))

    # 4d. 同一个任务被并发投递两次（重投/双 hook）：只允许 1 个 run
    notifier_mod._DELIVERED_LOCAL.clear()
    notifier_mod._DELIVERED_ORDER.clear()
    mw_c, client_c = make_notifier("idle", {"values": {"messages": []}})
    mw_d = notifier_mod.CompletionNotifierMiddleware(subagent_name="cbam_reporting")
    for attr in ("parent_thread_id", "parent_assistant_id", "parent_user_identity", "notify_url"):
        setattr(mw_d, attr, getattr(mw_c, attr))
    notifier_mod.get_client = lambda url, headers: client_c  # type: ignore[assignment]
    await asyncio.gather(
        mw_c._send_notification("[Async subagent 'cbam_reporting' has completed] Result: A", "t7"),
        mw_d._send_notification("[Async subagent 'cbam_reporting' has completed] Result: A", "t7"),
    )
    check("同一任务并发投递：只开 1 个 run", len(client_c.runs.created) == 1, str(len(client_c.runs.created)))

    # 5. 未知中断 → 不碰
    mw, client = make_notifier("interrupted", {"interrupts": [{"value": {"kind": "some_future_thing"}}]})
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
