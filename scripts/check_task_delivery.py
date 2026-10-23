"""自检：后台任务结果投递（发件箱 + 主管 hook）不依赖 LLM、不依赖 Redis。

跑法：``PYTHONPATH=. PYTHONUTF8=1 uv run python scripts/check_task_delivery.py``

架构（ADR-0010）：
    子 run 终态 → ``finalize_run`` 写发件箱行（pending）
                ├─ 父线程空闲 → ``custom/outbox_dispatcher.py`` 开一轮唤醒 run
                └─ 父线程在跑 → 该 run 的 before_agent 把结果写进 state 并 ack

覆盖：
1. guard 的 before_agent：终态未投递才写、投递过不再写、工具路径已写终态要补投；
   **刻意不实现 after_agent**——run 结束时写的模型已没机会转述，交给分发者开唤醒 run；
2. 判重键是 `(task_id, run_id)`，且 check 过的任务（delivered_run_id + 发件箱行 ack）
   不再重复投、分发者也不再开唤醒 run；
3. 投递文本统一由 `custom/outbox.py` 生成（分发者与 guard 必须逐字一致）；
4. 同批多个任务各自独立 ack —— 旧版 8s 让路窗口挡住的地方。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from custom.outbox import completion_text, shorten
from graphs.async_task_guard import AsyncTaskGuard, _completion_text

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'✓' if ok else '✗'} {name}{'' if ok else f'  ← {detail}'}")
    if not ok:
        FAILS.append(name)


# ---------- 假客户端：guard 侧 ----------


class FakeRuns:
    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses

    async def get(self, thread_id: str, run_id: str) -> dict:
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
    check("delivered_run_id 已记 → 不再写", await guard.abefore_agent(state2, None) is None)

    # 3. 投递消息在对话里 → 不再写（跨进程/跨 run 的判据）
    guard3, _ = make_guard({}, {})
    delivered = "[Async subagent 'cbam_reporting' has completed] Result: x\n\ntask_id: t3\nrun_id: run-t3"
    state3 = {"async_tasks": {"t3": task("t3", "success")}, "messages": [HumanMessage(content=delivered)]}
    check("消息里已有投递记录 → 不再写", await guard3.abefore_agent(state3, None) is None)

    # 4. 工具路径已把 state 写成终态但没投递 → 补投
    guard4, _ = make_guard({}, {})
    state4 = {"async_tasks": {"t4": task("t4", "success")}, "messages": []}
    check("state 已是终态但未投递 → 补投", bool(((await guard4.abefore_agent(state4, None)) or {}).get("messages")))

    # 5. 还在跑 → 什么都不做
    guard5, _ = make_guard({"run-t5": "running"}, {})
    check("running → 不动", await guard5.abefore_agent({"async_tasks": {"t5": task("t5", "running")}}, None) is None)

    # 6. 摘要超长 → 带截断标记（模型才知道该去 check 取全文）
    guard6, _ = make_guard({"run-t6": "success"}, {"t6": "表" * 900})
    out6 = await guard6.abefore_agent({"async_tasks": {"t6": task("t6", "running")}, "messages": []}, None)
    text6 = (out6 or {})["messages"][0].content
    check("超长摘要带截断标记", "结果已截断" in text6 and "check_async_task" in text6, text6[-60:])

    # 7. 模型自己 check 到终态结果 → 记 delivered_run_id + ack 发件箱行（之后不再投）
    guard7, _ = make_guard({"run-t7": "success"}, {"t7": "x"})
    claims7: list[str] = []

    async def claim7(task_: dict, parent: str) -> bool:
        claims7.append(task_["task_id"])
        return True

    guard7._claim_async = claim7  # type: ignore[method-assign]
    checked = Command(update={"messages": [], "async_tasks": {"t7": {**task("t7", "success")}}})
    request = type(
        "Req", (), {"tool_call": {"args": {"task_id": "t7"}}, "state": {"async_tasks": {"t7": task("t7", "running")}}}
    )()
    await guard7._amark_checked(request, checked)
    check(
        "check 到终态结果 → 记 delivered_run_id",
        (checked.update["async_tasks"]["t7"]).get("delivered_run_id") == "run-t7",
    )
    check("check 到终态结果 → 同时 ack 发件箱行（分发者不会再开唤醒 run）", claims7 == ["t7"], str(claims7))
    check(
        "check 过之后不再补投",
        await guard7.abefore_agent({"async_tasks": checked.update["async_tasks"], "messages": []}, None) is None,
    )

    # 7b. check 到非终态 → 既不记号也不 ack（发件箱行还没入账）
    guard7b, _ = make_guard({"run-t7b": "running"}, {})
    claims7b: list[str] = []

    async def claim7b(task_: dict, parent: str) -> bool:
        claims7b.append(task_["task_id"])
        return True

    guard7b._claim_async = claim7b  # type: ignore[method-assign]
    running = Command(update={"messages": [], "async_tasks": {"t7b": {**task("t7b", "running")}}})
    await guard7b._amark_checked(request, running)
    check("check 到运行中 → 不记号、不 ack", claims7b == [] and "delivered_run_id" not in running.update["async_tasks"]["t7b"])

    # 8. ack 失败（分发者或别的 run 已经投了）→ 不写投递消息
    guard8, _ = make_guard({"run-t8": "success"}, {"t8": "x"}, claimed=False)
    out8 = await guard8.abefore_agent({"async_tasks": {"t8": task("t8", "running")}, "messages": []}, None)
    check("ack 失败 → 不写投递消息", not (out8 or {}).get("messages"), str(out8))
    check(
        "ack 失败 → 不记 delivered_run_id",
        not (out8 or {}).get("async_tasks", {}).get("t8", {}).get("delivered_run_id"),
        str(out8),
    )

    # 9. ack 成功但写 state 失败 → 撤销 ack（行退回 pending，分发者接手）
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
    check("写 state 失败 → 撤销 ack", releases == ["t9"], str(releases))


    # 10. 故意不实现 after_agent：run 结束时不写，留给分发者开唤醒 run
    guard10, _ = make_guard({"run-t10": "success"}, {"t10": "x"})
    state10 = {"async_tasks": {"t10": task("t10", "running")}, "messages": []}
    from langchain.agents.middleware import AgentMiddleware

    check("after_agent 未被实现（基类空实现）", AsyncTaskGuard.aafter_agent is AgentMiddleware.aafter_agent)
    check("run 结束时不写 → 结果留给分发者", await guard10.aafter_agent(state10, None) is None)


def outbox_checks() -> None:
    """文本口径：分发者与 guard 共用同一份，不能各写一份。"""
    text = completion_text("t1", "run-t1", "cbam_reporting", "success", "x")
    guard_side = _completion_text(task("t1", "success"), "x")
    check("分发者与 guard 的投递文本逐字一致", text == guard_side, f"{text!r} != {guard_side!r}")
    check("非 success 也带前缀", completion_text("t1", "r", "a", "error", "x").startswith("[Async subagent"))
    check("摘要超长被截断", shorten("表" * 900).endswith(shorten("表" * 900)[-20:]) and len(shorten("表" * 900)) < 900)


async def main() -> None:
    await guard_checks()
    outbox_checks()
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        sys.exit(1)
    print("全部通过 ✔")


if __name__ == "__main__":
    asyncio.run(main())
