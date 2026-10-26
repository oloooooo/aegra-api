"""自检：后台任务结果投递（发件箱 + 主管 hook）不依赖 LLM、不依赖 Redis。

跑法：``PYTHONPATH=. PYTHONUTF8=1 uv run python scripts/check_task_delivery.py``

架构（ADR-0010）：
    子 run 终态 → ``finalize_run`` 写发件箱行（pending）
                ├─ 父线程空闲 → ``custom/service/outbox_dispatcher.py`` 开一轮唤醒 run
                └─ 父线程在跑 → 该 run 的 before_agent 把结果写进 state 并 ack

覆盖：
1. guard 的 before_agent：终态未投递才写、投递过不再写、工具路径已写终态要补投；
   **刻意不实现 after_agent**——run 结束时写的模型已没机会转述，交给分发者开唤醒 run；
2. 判重键是 `(task_id, run_id)`，且 check 过的任务（delivered_run_id + 发件箱行 ack）
   不再重复投、分发者也不再开唤醒 run；
3. 投递文本统一由 `shared/outbox.py` 生成（分发者与 guard 必须逐字一致）；
4. 同批多个任务各自独立 ack —— 旧版 8s 让路窗口挡住的地方。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from shared.outbox import completion_text, is_long_result, result_body
from shared.outbox import task_status_of_run as _status_of_run
from graphs.middleware.async_task_guard import AsyncTaskGuard, _completion_text

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
    statuses: dict[str, str], texts: dict[str, str], claimed: bool | None = True
) -> tuple[AsyncTaskGuard, FakeClient]:
    guard = AsyncTaskGuard({"cbam_reporting": {"url": "http://fake", "graph_id": "cbam_reporting"}})
    client = FakeClient(statuses, texts)
    guard._async_client = lambda spec: client  # type: ignore[method-assign]
    guard._parent_thread_id = lambda: "parent"  # type: ignore[method-assign]

    async def claim(task: dict, parent: str) -> bool | None:
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
    check(
        "投递文本以 [Async subagent 开头（前端认它是子 agent 返回）",
        text.startswith("[Async subagent 'cbam_reporting'"),
    )
    check("投递文本带 task_id/run_id 标记", "task_id: t1" in text and "run_id: run-t1" in text)
    check("投递文本带结果摘要", "12.3" in text)
    updated = (out or {}).get("async_tasks", {}).get("t1", {})
    check("同时把 state 里的状态刷成 success", updated.get("status") == "success")
    check("并在 task 上记 delivered_run_id", updated.get("delivered_run_id") == "run-t1")

    # 2. delivered_run_id 是唯一路内判据：记过就不再投
    state2 = {"async_tasks": {"t1": {**task("t1", "success"), "delivered_run_id": "run-t1"}}, "messages": []}
    check("delivered_run_id 已记 → 不再写", await guard.abefore_agent(state2, None) is None)
    state2b = {"async_tasks": {"t1": {**task("t1", "success"), "run_id": "run-t1b", "delivered_run_id": "run-t1"}}}
    check("run_id 变了 → 再投一次", bool(((await guard.abefore_agent(state2b, None)) or {}).get("messages")))

    # 3. 对话里已有投递卡片但 state 没记 → 仍然补投（消息不可信，宁可重复不可漏）
    guard3, _ = make_guard({}, {})
    card = "[Async subagent 'cbam_reporting' has completed] Result: x\n\ntask_id: t3\nrun_id: run-t3"
    state3 = {"async_tasks": {"t3": task("t3", "success")}, "messages": [HumanMessage(content=card)]}
    out3 = await guard3.abefore_agent(state3, None)
    check("卡片在对话里也不再作为判据（摘要会删掉它）", bool((out3 or {}).get("messages")), str(out3))

    # 3b. 模型复述带同样前缀 + task_id/run_id 的句子，不能把真结果挡掉
    guard3b, _ = make_guard({}, {})
    forged = HumanMessage(
        content="刚才那次是 [Async subagent 'cbam_reporting' has completed] Result: 假的\n\ntask_id: t3b\nrun_id: run-t3b"
    )
    state3b = {"async_tasks": {"t3b": task("t3b", "success")}, "messages": [forged]}
    check("伪造前缀不导致漏投", bool(((await guard3b.abefore_agent(state3b, None)) or {}).get("messages")))

    # 4. 工具路径已把 state 写成终态但没投递 → 补投
    guard4, _ = make_guard({}, {})
    state4 = {"async_tasks": {"t4": task("t4", "success")}, "messages": []}
    check("state 已是终态但未投递 → 补投", bool(((await guard4.abefore_agent(state4, None)) or {}).get("messages")))

    # 5. 还在跑 → 什么都不做
    guard5, _ = make_guard({"run-t5": "running"}, {})
    check("running → 不动", await guard5.abefore_agent({"async_tasks": {"t5": task("t5", "running")}}, None) is None)

    # 6. 结果超长 → 全文进卡片，不截断（模型一次看到全量，不用再调 check）
    guard6, _ = make_guard({"run-t6": "success"}, {"t6": "表" * 900})
    out6 = await guard6.abefore_agent({"async_tasks": {"t6": task("t6", "running")}, "messages": []}, None)
    text6 = (out6 or {})["messages"][0].content
    check("超长结果不截断（全文都在卡片里）", "表" * 900 in text6, f"len={len(text6)}")
    check("不截断就不带截断标记", "截断" not in text6, text6[-60:])
    check("超过 3000 字要告警（用真实长度判定）", is_long_result("表" * 3001) and not is_long_result("表" * 3000))

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
    check(
        "check 到运行中 → 不记号、不 ack",
        claims7b == [] and "delivered_run_id" not in running.update["async_tasks"]["t7b"],
    )

    # 8. claim 说别人已投（False）→ 不写消息，但把"查过了"写回 state，下次不再问
    guard8, _ = make_guard({"run-t8": "success"}, {"t8": "x"}, claimed=False)
    out8 = await guard8.abefore_agent({"async_tasks": {"t8": task("t8", "running")}, "messages": []}, None)
    check("claim False → 不写投递消息", not (out8 or {}).get("messages"), str(out8))
    check(
        "claim False → 记下 delivered_run_id（自校准，下次 O(1) 跳过）",
        (out8 or {}).get("async_tasks", {}).get("t8", {}).get("delivered_run_id") == "run-t8",
        str(out8),
    )
    claims8: list[str] = []

    async def claim8(task_: dict, parent: str) -> bool:
        claims8.append(task_["task_id"])
        return False

    guard8._claim_async = claim8  # type: ignore[method-assign]
    state8b = {"async_tasks": (out8 or {}).get("async_tasks") or {}, "messages": []}
    await guard8.abefore_agent(state8b, None)
    check("自校准之后不再打 claim", claims8 == [], str(claims8))

    # 8b. claim 结果未知（None）→ 不投：行留在 pending，下一轮再试（多投会重复，漏投才致命）
    guard8b, _ = make_guard({"run-t8b": "success"}, {"t8b": "x"}, claimed=None)
    out8b = await guard8b.abefore_agent({"async_tasks": {"t8b": task("t8b", "running")}, "messages": []}, None)
    check("claim 不确定 → 不写投递消息", not (out8b or {}).get("messages"), str(out8b))
    check(
        "claim 不确定 → 也不记 delivered_run_id（下一轮还要再试）",
        "delivered_run_id" not in (out8b or {}).get("async_tasks", {}).get("t8b", {}),
        str(out8b),
    )

    # 9. 查摘要失败 → 根本没 claim（慢 await 前置），行不受影响，交给分发者
    guard9, _ = make_guard({"run-t9": "success"}, {"t9": "x"})
    claims9: list[str] = []
    releases: list[str] = []

    async def claim9(task_: dict, parent: str) -> bool:
        claims9.append(task_["task_id"])
        return True

    async def release(task_: dict, parent: str) -> None:
        releases.append(task_["task_id"])

    async def broken_summary(task_: dict) -> str:
        raise RuntimeError("summary failed")

    guard9._claim_async = claim9  # type: ignore[method-assign]
    guard9._release_async = release  # type: ignore[method-assign]
    guard9._summary_async = broken_summary  # type: ignore[method-assign]
    with contextlib.suppress(RuntimeError):
        await guard9.abefore_agent({"async_tasks": {"t9": task("t9", "running")}, "messages": []}, None)
    check("慢 await 在 claim 之前：查摘要失败时还没 claim", claims9 == [], str(claims9))
    check("没 claim 就不用 release", releases == [], str(releases))

    # 9b. 取消（cancelled）：只发状态通知，不当结果投、不占发件箱行
    guard9b, _ = make_guard({}, {})
    claims9b: list[str] = []

    async def claim9b(task_: dict, parent: str) -> bool:
        claims9b.append(task_["task_id"])
        return True

    guard9b._claim_async = claim9b  # type: ignore[method-assign]
    state9b = {"async_tasks": {"t9b": {**task("t9b", "cancelled")}}, "messages": []}
    out9b = await guard9b.abefore_agent(state9b, None)
    texts9b = [m.content for m in (out9b or {}).get("messages") or []]
    check("cancelled → 写一条状态通知", len(texts9b) == 1, str(texts9b))
    check("状态通知不带 Result:", "Result:" not in texts9b[0], texts9b[0])
    check("状态通知文案标明 cancelled", "was cancelled" in texts9b[0], texts9b[0])
    check("cancelled 不去抢发件箱行", claims9b == [], str(claims9b))
    check(
        "cancelled 记 notified_run_id，不写 delivered_run_id",
        (out9b or {}).get("async_tasks", {}).get("t9b", {}).get("notified_run_id") == "run-t9b"
        and "delivered_run_id" not in (out9b or {}).get("async_tasks", {}).get("t9b", {}),
        str(out9b),
    )

    # 9c. 同一条通知不重复发；但换了 run 就要重新通知
    check(
        "已通知过 → 不再重复",
        await guard9b.abefore_agent({"async_tasks": (out9b or {}).get("async_tasks") or {}, "messages": []}, None)
        is None,
    )
    rerun = {"async_tasks": {"t9b": {**task("t9b", "cancelled"), "run_id": "run-t9b-2", "notified_run_id": "run-t9b"}}}
    check("换了 run → 重新通知", bool(((await guard9b.abefore_agent(rerun, None)) or {}).get("messages")))

    # 9d. 服务端 interrupted_by 映射成两个词
    check(
        "interrupted_by=cancel → cancelled",
        _status_of_run({"status": "interrupted", "interrupted_by": "cancel"}) == "cancelled",
    )
    check(
        "interrupted_by=hitl → interrupted",
        _status_of_run({"status": "interrupted", "interrupted_by": "hitl"}) == "interrupted",
    )
    check("其他状态原样通过", _status_of_run({"status": "success"}) == "success")

    # 9e. check 到 cancelled 不算"已入上下文"（没有结果可记）
    guard9e, _ = make_guard({}, {})
    cancelled_cmd = Command(update={"messages": [], "async_tasks": {"t9e": {**task("t9e", "cancelled")}}})
    await guard9e._amark_checked(request, cancelled_cmd)
    check(
        "check 到 cancelled → 不记 delivered_run_id",
        "delivered_run_id" not in cancelled_cmd.update["async_tasks"]["t9e"],
        str(cancelled_cmd.update),
    )

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
    check("投递正文不截断", result_body("表" * 900) == "表" * 900)


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
