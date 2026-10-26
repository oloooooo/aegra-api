"""FastAPI 依赖：获取数据库 session。

复用 aegra_api.core.orm 中已有的 session 工厂，避免重复创建 engine/sessionmaker。
"""

from typing import Annotated
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from aegra_api.core.orm import  get_session
from custom.service.account import _decode_access_token
from custom.db.repositories import UserRepository

# __all__ = ["get_user_by_token","get_super_user"]

# async def get_user_by_token(credentials: Annotated[HTTPAuthorizationCredentials | None,
#                             Depends(HTTPBearer(auto_error=False))],
#                             session: Annotated[AsyncSession, Depends(get_session)],):
#     if credentials is None:
#         raise HTTPException(
#         status_code=status.HTTP_401_UNAUTHORIZED,
#         detail="无效或已过期的令牌",
#         headers={"WWW-Authenticate": "Bearer"},
#     )
#     user_id =  _decode_access_token(credentials.credentials)
#     user = await UserRepository(session).get(user_id)
#     if not user:
#         raise HTTPException(
#         status_code=status.HTTP_404_NOT_FOUND,
#         detail="该用户不存在",
#         headers={"WWW-Authenticate": "Bearer"},
#     )
#     return user

# async def get_super_user(credentials: Annotated[HTTPAuthorizationCredentials | None,
#                             Depends(HTTPBearer(auto_error=False))],
#                             session: Annotated[AsyncSession, Depends(get_session)]):
#     user = await get_user_by_token(credentials,session)
#     return user.is_super