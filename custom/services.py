import time

import bcrypt
from fastapi import HTTPException, status
import jwt
import os
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from custom import project_config
from custom.models import UserSpace

# access token 有效期（秒）；非密钥，配在 config.yaml 的 auth.access_token_ttl
TOKEN_TTL = int(project_config.get("auth.access_token_ttl"))


def _hash_password(password: str) -> str:
    # pepper 来自 .env，与 bcrypt 盐（自动生成、嵌在哈希串中）叠加
    peppered = (password + os.environ["PASSWORD_PEPPER"]).encode()
    return bcrypt.hashpw(peppered, bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    peppered = (password + os.environ["PASSWORD_PEPPER"]).encode()
    return bcrypt.checkpw(peppered, hashed.encode())


def _create_access_token(identity: str) -> str:
    now = int(time.time())
    payload = {"sub": identity, "iat": now, "exp": now + TOKEN_TTL}
    return jwt.encode(payload, os.environ["JWT_SECRET"], algorithm="HS256")


# 内部服务账号（async subagent / 前端代理读取子线程用）；非密钥，配在 config.yaml
SERVICE_IDENTITY = project_config.get("auth.service_identity")
_service_token_cache: str | None = None


def service_token() -> str:
    """服务身份的长期 token（进程级缓存）。

    custom_auth 对 SERVICE_IDENTITY 免 DB 直接放行；子 agent 的 thread 属主就是它，
    所以只有它能读子线程/订阅子 run。shortcut: 10 年有效、与服务同生共死；
    要轮换时改 env 或改成启动时铸造。
    """
    global _service_token_cache
    if _service_token_cache is None:
        now = int(time.time())
        _service_token_cache = jwt.encode(
            {"sub": SERVICE_IDENTITY, "iat": now, "exp": now + 10 * 365 * 24 * 3600},
            os.environ["JWT_SECRET"],
            algorithm="HS256",
        )
    return _service_token_cache


def _decode_access_token(token: str) -> str:
    """
    接收access token
    返回user_id
    """
    payload = jwt.decode(
        token,
        os.environ["JWT_SECRET"],
        algorithms=["HS256"],  # 必须显式指定，避免 alg 混淆攻击
        options={"require": ["sub", "iat", "exp"]},
        leeway=0,  # 可按需给时钟偏移留几秒，例如 leeway=5
    )
    if payload is None or payload.get("type") or not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或已过期的令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    identity = payload.get("sub")
    if not isinstance(identity, str) or not identity:
        raise jwt.InvalidTokenError("Invalid 'sub' claim")
    return identity


async def count_user_spaces(session: AsyncSession, identity: str) -> int:
    """用户加入了多少个微服务空间。"""
    stmt = select(func.count()).where(UserSpace.identity == identity)
    return (await session.execute(stmt)).scalar_one()


async def count_space_users(session: AsyncSession, name: str) -> int:
    """一个微服务空间有多少用户。"""
    stmt = select(func.count()).where(UserSpace.space_name == name)
    return (await session.execute(stmt)).scalar_one()