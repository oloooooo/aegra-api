import time

import bcrypt
from fastapi import HTTPException, status
import jwt
import os
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from custom.models import UserSpace

# access token 有效期（秒），改 .env 可覆盖
TOKEN_TTL = int(os.environ.get("ACCESS_TOKEN_TTL", "3600"))


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