import os

import jwt
import structlog
from langgraph_sdk import Auth
from langgraph_sdk.auth.exceptions import HTTPException as AuthHTTPException
from sqlalchemy import select

from aegra_api.core.database import db_manager
from aegra_api.core.orm import Thread as ThreadORM
from aegra_api.core.orm import get_session_maker
from custom.quota import MAX_THREADS_PER_USER
from custom.repositories import ThreadQuotaRepository, UserRepository, UserSpaceRepository

logger = structlog.getLogger(__name__)

# 内部服务账号（async subagent 的静态 token）：跳过 DB 查询直接放行。
# subagent 拿不到终端用户的 JWT，只能用构建期铸的静态 token，见 graphs/deepagents_nodes.py。
SERVICE_IDENTITY = os.environ.get("SERVICE_IDENTITY", "subagent_service")

auth = Auth()

@auth.authenticate
async def _decode_access_token(headers: dict[str, str]):
    """
    aegra 中间件按位置传入整个 headers 字典（不是按名注入 token），
    这里自己从 Authorization: Bearer <token> 里取 token。
    """
    scheme, _, token = (headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthHTTPException(headers={"WWW-Authenticate": "Bearer"})
    maker = get_session_maker()
    async with maker() as session:
        user_repo = UserRepository(session=session)
        user_space_repo = UserSpaceRepository(session=session)
        try:
            payload = jwt.decode(
                token,
                os.environ["JWT_SECRET"],
                algorithms=["HS256"],  # 必须显式指定，避免 alg 混淆攻击
                options={"require": ["sub", "iat", "exp"]},
                leeway=5,  # 可按需给时钟偏移留几秒，例如 leeway=5
            )
        except jwt.InvalidTokenError:
            raise AuthHTTPException(
                detail="无效或已过期的令牌", headers={"WWW-Authenticate": "Bearer"}
            )
        if payload.get("type") or not payload.get("sub"):
            raise AuthHTTPException(
                detail="无效或已过期的令牌", headers={"WWW-Authenticate": "Bearer"}
            )
        identity = payload["sub"]
        if not isinstance(identity, str) or not identity:
            raise AuthHTTPException(
                detail="无效或已过期的令牌", headers={"WWW-Authenticate": "Bearer"}
            )
        if identity == SERVICE_IDENTITY:
            return {
                "identity": identity,
                "display_name": "Subagent Service",
                "permissions": ["read"],
                "is_authenticated": True,
                "user_spaces": [],
            }
        user_table = await user_repo.get(identity=identity)
        user_spaces =await user_space_repo.list_by_user(identity=identity)
        if user_table is None:
            raise AuthHTTPException(
                detail="用户不存在", headers={"WWW-Authenticate": "Bearer"}
            )
        return {
            "identity": identity,
            "display_name": user_table.display_name,
            "permissions": ["read", "write"] if user_table.is_super else ["read"],
            "is_authenticated": True,
            "user_spaces":user_spaces
        }


# ---------- 配额：一个用户能拥有多少个会话（ADR-0012） ----------
#
# 这是替代 MAX_ACTIVE_TASKS 的那道闸：不再限制一个主管派多少子 agent，而是限制
# 用户拥有多少 thread（子线程也计数，它们的 metadata 带 owner_identity）。
# 超管不受限：管理员要能排查别人的问题，且总数应当能一眼数清。
@auth.on.threads.create
async def _quota_threads_create(ctx, value):
    if "write" in (ctx.permissions or []):
        return None
    maker = get_session_maker()
    async with maker() as session:
        used = await ThreadQuotaRepository(session).count_owned(ctx.user.identity)
    if used >= MAX_THREADS_PER_USER:
        raise AuthHTTPException(
            status_code=403,
            detail=(
                f"会话数已达上限（{MAX_THREADS_PER_USER}），请先删除不用的会话再新建"
            ),
        )
    # 把属主写进 metadata：子线程的属主是服务账号，配额靠这个字段计数
    meta = value.get("metadata") or {}
    if not meta.get("owner_identity"):
        value["metadata"] = {**meta, "owner_identity": ctx.user.identity}
    return None



# 删会话时级联删它的子线程（ADR-0012）。
# 子线程的属主是服务账号，用户既看不见也删不掉；不跟着删就是永久孤儿，
# 一直占配额与 checkpoint 存储。这里删的是子线程，父线程交给原生接口继续删。
#
# 注意顺序：aegra 在**校验 ownership 之前**调用本 handler，所以这里要自己确认
# 这个 thread 属于调用者，否则任何人传别人的 thread_id 都能触发级联删除。
@auth.on.threads.delete
async def _cascade_child_threads(ctx, value):
    thread_id = (value or {}).get("thread_id")
    if not thread_id:
        return None  # prune 等批量路径不带单个 thread_id，跳过
    maker = get_session_maker()
    async with maker() as session:
        owner = await session.scalar(select(ThreadORM.user_id).where(ThreadORM.thread_id == thread_id))
        if owner is None or (owner != ctx.user.identity and "write" not in (ctx.permissions or [])):
            return None  # 交给原生接口去 404/拒绝，这里不动任何东西
        children = await ThreadQuotaRepository(session).delete_children(thread_id)
    if children:
        checkpointer = db_manager.get_checkpointer()
        for child in children:
            await checkpointer.adelete_thread(child)  # checkpoint 没有外键，要自己清
    logger.info("thread_children_deleted", parent=thread_id, children=len(children))
    return None
