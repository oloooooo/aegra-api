"""账号密码与 token 校验（HTTP 侧）：bcrypt+pepper 哈希、token -> identity、空间计数。

签发在 `shared/token.py`（图侧也要用）；这里只放“失败要映射成 HTTP 异常”的部分。
"""

import os

import bcrypt
from fastapi import HTTPException, status
import jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from custom.db.models import UserSpace


def _hash_password(password: str) -> str:
    # pepper 来自 .env，与 bcrypt 盐（自动生成、嵌在哈希串中）叠加
    peppered = (password + os.environ["PASSWORD_PEPPER"]).encode()
    return bcrypt.hashpw(peppered, bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    peppered = (password + os.environ["PASSWORD_PEPPER"]).encode()
    return bcrypt.checkpw(peppered, hashed.encode())


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