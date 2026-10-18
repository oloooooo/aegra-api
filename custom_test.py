"""端到端测试：supervisor(deepagents_nodes) 按领域把活派给子 agent 对话。

前置：aegra.json 已注册 supervisor + 3 个领域子 agent，且 **后端要重启**（图注册表在启动时读）：
    uv run python -u src/aegra_api/main.py
{
  "graphs": {
    "supervisor": "./graphs/deepagents_nodes.py:graph",
    "cbam_reporting": "./graphs/domain_agents.py:graph_cbam_reporting",
    "hybrid_bonding": "./graphs/domain_agents.py:graph_hybrid_bonding",
    "sbol3_synbio": "./graphs/domain_agents.py:graph_sbol3_synbio"
  }
}

账号：root / 1234（库里已有；登录接口换 token，不再硬编码过期 JWT）。

跑：
    uv run python custom_test.py          # 进入交互，直接回车用默认问题
    uv run python custom_test.py "问题"   # 带首个问题启动

预期：supervisor 选领域子 agent（如 CBAM 表 → cbam_reporting），子 agent 跑完
经 CompletionNotifierMiddleware 主动回写，这里能流式看到过程与最终回答。
"""

import os
import sys
import time

import httpx
from langgraph_sdk import get_sync_client

# Windows 控制台默认 GBK，模型输出含 ₂/℃ 之类字符会 UnicodeEncodeError 直接崩
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = os.environ.get("AEGRA_URL", "http://127.0.0.1:2026")
IDENTITY = os.environ.get("TEST_IDENTITY", "root")
PASSWORD = os.environ.get("TEST_PASSWORD", "1234")

assistant_id = "supervisor"
DEFAULT_PROMPT = "查一下 /cbam-reporting/cn_goods_rules.xlsx：有哪些 sheet？默认排放因子里铝的因子是多少？"


def _login() -> str:
    """用账号密码换 access token；后端没起/账号错都给明确报错。"""
    try:
        resp = httpx.post(
            f"{BASE_URL}/custom/login",
            json={"identity": IDENTITY, "password": PASSWORD},
            timeout=10,
        )
    except httpx.HTTPError as e:
        raise SystemExit(f"连不上后端 {BASE_URL}（先起服务：uv run python -u src/aegra_api/main.py）：{e}") from e
    if resp.status_code != 200:
        raise SystemExit(f"登录失败 {resp.status_code}：{resp.text}")
    return resp.json()["access_token"]


headers = {"Authorization": f"Bearer {_login()}"}
client = get_sync_client(url=BASE_URL, headers=headers)
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
    """跑一轮（到这里只是「派活完成」），返回本轮最后一条 ai 消息。"""
    for part in client.runs.stream(
        thread_id,
        assistant_id,
        input={"messages": [{"role": "user", "content": prompt}]},
        stream_mode=["messages", "custom", "updates"],
    ):
        _print_part(part)
    print()
    msgs = client.threads.get_state(thread_id, headers=headers)["values"]["messages"]
    return next(m["content"] for m in reversed(msgs) if m.get("type") == "ai")


def _wait_for_callback(thread_id: str, timeout: int = 180) -> None:
    """等子 agent 回写（CompletionNotifierMiddleware 会唤醒主管再跑一轮）。

    主管派完活本轮 run 就结束了，结果在后来的 run 里才出现；这里轮询线程状态，
    一有新消息就打印，直到最后一条是带内容的 ai 消息或超时。
    """
    seen = len(client.threads.get_state(thread_id, headers=headers)["values"]["messages"])
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        msgs = client.threads.get_state(thread_id, headers=headers)["values"]["messages"]
        for m in msgs[seen:]:
            content = str(m.get("content") or "")
            if content and not m.get("tool_calls"):
                print(f"\n[{m.get('type')}] {content}", flush=True)
        grew = len(msgs) > seen
        seen = len(msgs)
        if grew and str(msgs[-1].get("content") or "").strip() and not msgs[-1].get("tool_calls"):
            return
    print("\n（等子 agent 回写超时，可用 check_async_task 追问）", flush=True)


def main() -> None:
    thread_id = client.threads.create(headers=headers)["thread_id"]
    print("thread_id =", thread_id, flush=True)
    first = sys.argv[1] if len(sys.argv) > 1 else ""
    while True:
        user_input = first or input("User:").strip()
        first = ""
        if user_input == "exit":
            break
        prompt = user_input or DEFAULT_PROMPT
        _ask(thread_id, prompt)
        _wait_for_callback(thread_id)


if __name__ == "__main__":
    main()
