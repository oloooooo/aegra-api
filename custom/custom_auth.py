import os

import jwt
from langgraph_sdk import Auth
from langgraph_sdk.auth.exceptions import HTTPException as AuthHTTPException
import structlog
from aegra_api.core.orm import get_session_maker
from custom.repositories import UserRepository,UserSpaceRepository

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
