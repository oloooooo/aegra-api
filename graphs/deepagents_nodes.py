from typing import Annotated

from deepagents import AsyncSubAgent, create_deep_agent
from deepagents.middleware import FilesystemPermission
import dotenv
import os
import time

from pathlib import Path

import jwt
import structlog

from graphs.llms import model
from pydantic import BaseModel

dotenv.load_dotenv()


class deepAgentContext(BaseModel):
    user_kind: str | None = None


from langchain.tools import InjectedToolArg, tool, ToolRuntime
from langgraph.graph.state import RunnableConfig
from langchain.agents.middleware import TodoListMiddleware
from graphs.async_task_guard import AsyncTaskGuard
from graphs.space_backend import SpaceBackend
from graphs.prompts import (
    MAIN_SYSTEM_PROMPT,
    CBAM_SUBAGENT_DESCRIPTION,
    HYBRID_BONDING_SUBAGENT_DESCRIPTION,
    SBOL3_SUBAGENT_DESCRIPTION,
)
from graphs.tools import MAIN_TOOLS

logger = structlog.getLogger(__name__)


@tool(
    description="该工具可以返回，用户id，用户名，用户权限，在无法获取相关内容的时候，返回错误信息"
)
async def get_user_data(
    config: Annotated[RunnableConfig, InjectedToolArg], runtime: ToolRuntime
) -> dict:
    try:
        """Tool that accesses the authenticated user."""
        auth_user = config[
            "configurable"
        ][  # pyright: ignore[reportTypedDictNotRequiredAccess]
            "langgraph_auth_user"
        ]
        if not isinstance(auth_user, dict):
            auth_user = auth_user.to_dict()
        assert auth_user is not None, "获取auth_user失败"
        logger.info(type(auth_user))
        user_id = auth_user["identity"]
        permissions = auth_user.get("permissions", [])
        user_name = auth_user.get("display_name", "")
        user_spaces = auth_user.get("user_spaces", [])
        return {
            "user_id": user_id,
            "permissions": permissions,
            "user_name": user_name,
            "user_spaces": user_spaces,
        }
    except Exception as e:
        return {"error": str(e)}


# 和子 agent（graphs/domain_agents.py）同一份 okf 根，虚拟路径才对得上
okf_dir = str(Path(__file__).resolve().parent / os.getenv("OKF_DIR", "okf"))

# 静态服务 token：subagent 的 runs.create 不带终端用户 JWT（deepagents 不转发），
# 构建期用 JWT_SECRET 铸一枚长期 token，custom_auth.py 对该 identity 免 DB 直接放行。
# shortcut: 10 年有效、与服务同生共死；要轮换时改 env 或改成启动时铸造。
_service_token = jwt.encode(
    {
        "sub": os.environ.get("SERVICE_IDENTITY", "subagent_service"),
        "iat": int(time.time()),
        "exp": int(time.time()) + 10 * 365 * 24 * 3600,
    },
    os.environ["JWT_SECRET"],
    algorithm="HS256",
)

# 领域子 agent：(agent 名 = graph_id, 绑定 space, 描述)。
# space 经拦截层单发给子 agent（guard._config_for），用户没有该 space 权限时
# start 直接拒绝；绑定只在 guard spec 里，deepagents 不读多余键。
_DOMAIN_SPECS = {
    "cbam_reporting": {
        "space": "cbam-reporting",
        "description": CBAM_SUBAGENT_DESCRIPTION,
    },
    "hybrid_bonding": {
        "space": "hybrid-bonding",
        "description": HYBRID_BONDING_SUBAGENT_DESCRIPTION,
    },
    "sbol3_synbio": {
        "space": "sbol3-synbio",
        "description": SBOL3_SUBAGENT_DESCRIPTION,
    },
}

_specs: dict[str, dict] = {
    name: AsyncSubAgent(  # pyright: ignore[reportCallIssue]
        name=name,
        description=meta["description"],
        graph_id=name,
        url="http://127.0.0.1:2026",
        headers={"Authorization": f"Bearer {_service_token}"},
        space=meta["space"],  # type: ignore[typeddict-unknown-key]  guard 专用
    )
    for name, meta in _DOMAIN_SPECS.items()
}

graph = create_deep_agent(
    model=model,
    system_prompt=MAIN_SYSTEM_PROMPT,
    tools=[*MAIN_TOOLS],
    # 按空间隔离的 backend：8 个 fs 工具单点兜住 allowed_spaces（ADR-0005）
    backend=SpaceBackend(root_dir=okf_dir),
    context_schema=deepAgentContext,
    # tools=[get_user_data],
    # 拆任务清单（write_todos）；guard 拦截 start/update_async_task：按 spec 绑定的
    # space 经 run config 转发给子 agent（ADR-0005）+ 并发上限
    middleware=[TodoListMiddleware(), AsyncTaskGuard(_specs)],
    subagents=[* _specs.values()],
    permissions=[FilesystemPermission(operations=["write"], paths=["/"], mode="deny")],
)
if __name__ == "__main__":
    message = {
        "messages": [
            {"role": "user", "content": "hello,表格消融实验1.xlsx中的CER，最低的有哪些"}
        ]
    }
    response = graph.invoke(message)  # pyright: ignore[reportArgumentType]
    print(response["messages"][-1]["content"])
