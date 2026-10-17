"""端到端测试：supervisor(deepagents_nodes) 把查表活派给 xlsx_process 子 agent。

前置：aegra.json 已注册 supervisor / xlsx_process 两个图，且 **后端要重启**（图注册表在启动时读）：
    python -u src/aegra_api/main.py
{
  "graphs": {
    "supervisor": "./graphs/deepagents_nodes.py:graph",
    "xlsx_process": "./graphs/xlsx_process.py:graph"
  }
}

跑：
    uv run python test_deepagents_xlsx.py
不知道为什么xlsx_process明明使用了中间层，子agent没有主动返回内容
"""

from langgraph_sdk import get_sync_client
import time


assistant_id = "supervisor"
PROMPT = "查一下 /消融实验1.xlsx：这张表有哪些列？再按 CER 从低到高给我前 3 行。"

headers = {
    "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJyb290IiwiaWF0IjoxNzkyMDQ3NDE4LCJleHAiOjE3OTIxMzM4MTh9.IVjvol8umd_jHBXjeP9GhQvfaIi5-6YYk2421OLTUyA"
}
client = get_sync_client(url="http://127.0.0.1:2026",headers=headers)
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
    msgs = client.threads.get_state(thread_id,headers=headers)["values"]["messages"]
    return next(m["content"] for m in reversed(msgs) if m.get("type") == "ai")


def main() -> None:
    thread_id = client.threads.create(headers=headers)["thread_id"]
    print("thread_id =", thread_id, flush=True)
    while True:
        user_input = input("User:")
        user_input = user_input.strip()
        if user_input == "exit":
            print(exit)
            break
        anwser = _ask(thread_id, user_input)
        print(anwser)


if __name__ == "__main__":
    main()