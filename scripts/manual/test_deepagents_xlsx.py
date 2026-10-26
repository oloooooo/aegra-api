"""端到端测试：supervisor(deepagents_nodes) 把查表活派给 xlsx_process 子 agent。

前置：aegra.json 已注册 supervisor / xlsx_process 两个图，且 **后端要重启**（图注册表在启动时读）：
    python -u src/aegra_api/main.py

跑：
    uv run python test_deepagents_xlsx.py
"""

from langgraph_sdk import get_sync_client
import time

client = get_sync_client(url="http://127.0.0.1:2026")
assistant_id = "supervisor"
PROMPT = "查一下 /消融实验1.xlsx：这张表有哪些列？再按 CER 从低到高给我前 3 行。"


_printed: dict[str, int] = {}


def _print_part(part: object) -> None:
    """messages 打增量，custom 打工具事件，其它只报事件名。"""
    event = getattr(part, "event", "")
    data = getattr(part, "data", None)
    if event in ("messages", "messages/partial"):
        # aegra 只发 messages/partial，且每条是“到目前为止的全部内容”，得自己算增量
        msg = (data or [None])[-1] if isinstance(data, list) else data
        text = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(text, str) and len(text) > _printed.get(msg["id"], 0):
            print(text[_printed.get(msg["id"], 0) :], end="", flush=True)
            _printed[msg["id"]] = len(text)
    elif event in ("custom", "updates", "messages/metadata", "messages/complete"):
        if event == "custom":  # 子 agent 在另一个 run 里，这里通常看不到
            print(f"\n[custom] {data}", flush=True)
    else:
        print(f"\n[{event}] {str(data)[:200]}", flush=True)


def _ask(thread_id: str, prompt: str) -> str:
    """跑一轮，边流边打，返回两条消息合并后的最终回答。"""
    for part in client.runs.stream(
        thread_id,
        assistant_id,
        input={"messages": [{"role": "user", "content": prompt}]},
        stream_mode=["messages", "custom", "updates"],
    ):
        _print_part(part)
    msgs = client.threads.get_state(thread_id)["values"]["messages"]
    return next(m["content"] for m in reversed(msgs) if m.get("type") == "ai")


def main() -> None:
    thread_id = client.threads.create()["thread_id"]
    print("thread_id =", thread_id, flush=True)

    # 第一轮：supervisor 只会派活并回 task_id（异步子 agent 的设计行为）
    print("\n=== 第一轮：派活 ===")
    print(_ask(thread_id, PROMPT))

    # 第二轮：异步子 agent 要跑十几秒，轮着问直到它落地
    print("\n=== 第二轮：取结果 ===")
    answer = ""
    for _ in range(6):
        time.sleep(15)
        answer = _ask(thread_id, "任务跑完了吗？跑完就把列名和 CER 最低的 3 行直接给我。")
        if "还在跑" not in answer and "running" not in answer.lower():
            break
    print(answer)


if __name__ == "__main__":
    main()
