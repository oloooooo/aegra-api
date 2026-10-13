from deepagents import AsyncSubAgent, create_deep_agent
from deepagents.middleware import FilesystemPermission
from deepagents.backends import FilesystemBackend
import dotenv
import os

from pathlib import Path

from graphs.llms import model
from pydantic import BaseModel

dotenv.load_dotenv()


class deepAgentContext(BaseModel):
    user_kind: str | None = None


# 和子 agent（xlsx_process）同一份 okf 根，虚拟路径才对得上
okf_dir = str(Path(__file__).resolve().parent / os.getenv("OKF_DIR", "okf"))
graph = create_deep_agent(
    model=model,
    backend=FilesystemBackend(root_dir=okf_dir),
    context_schema=deepAgentContext,
    subagents=[
        AsyncSubAgent(
            name="xlsx_process",
            description=(
                "查 xlsx 表格的专用 agent，跑在后台。任何关于表格内容的问题（有哪些 sheet/列、"
                "按条件筛行、全表找关键词、空值重复值画像）都交给它；路径用 /消融实验1.xlsx "
                "这样的虚拟路径。用 start_async_task 派活，需要结果时用 check_async_task。"
            ),
            graph_id="xlsx_process",
            url="http://127.0.0.1:2026",
        )
    ],
    permissions=[FilesystemPermission(operations=["write"], paths=["/"], mode="deny")],
)
if __name__ == "__main__":
    message = {"messages":[{"role":"user","content":"hello,表格消融实验1.xlsx中的CER，最低的有哪些"}]}
    response = graph.invoke(message)  # pyright: ignore[reportArgumentType]
    print(response["messages"][-1]["content"])