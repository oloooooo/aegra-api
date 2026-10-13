from deepagents import AsyncSubAgent, FilesystemPermission, create_deep_agent
from deepagents.backends import FilesystemBackend
import dotenv
import os

from pathlib import Path
from langchain_core.messages import HumanMessage
from graphs.llms import model
from pydantic import BaseModel
from langchain_quickjs import CodeInterpreterMiddleware

from graphs.xlsx_tools import XLSX_TOOLS

dotenv.load_dotenv()

okf_dir = str(Path(__file__).resolve().parent / os.getenv("OKF_DIR", "okf"))


graph = create_deep_agent(
    model=model,
    middleware=[
        CodeInterpreterMiddleware(
            mode="turn",
            ptc=XLSX_TOOLS,# pyright: ignore[reportArgumentType]
            timeout = 120,
            max_result_chars=10000
        )
    ],  
    backend= FilesystemBackend(root_dir=okf_dir),
    permissions=[FilesystemPermission(operations=['write'],paths=["/"],mode='deny')]
)

if __name__ == "__main__":
    # 跑：uv run python -m graphs.xlsx_process
    prompt = "看下 /消融实验1.xlsx 有哪些 sheet 和列，再按 CER 从低到高列前 5 行"
    for mode, payload in graph.stream(  # pyright: ignore[reportArgumentType]
        {"messages": [HumanMessage(content=prompt)]},
        stream_mode=["messages", "custom"],
    ):
        if mode == "custom":  # xlsx 工具的 start/done/error
            print(f"\n[event] {payload}", flush=True)
        else:
            chunk, _meta = payload
            if chunk.content:
                print(chunk.content, end="", flush=True)
    print()