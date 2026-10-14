"""注册/登录路由：bcrypt 加盐哈希 + pepper 存储密码，登录签发 JWT access token。"""

import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from aegra_api.api.runs import logger
from custom.models import MicroServiceSpace, UserTables
from custom.repositories import MicroServiceSpaceRepository, UserRepository, UserWorkspaceRepository
from custom.dependencies import get_session, get_super_user, get_user_by_token
from custom.schemas import CreateSpaceInput, CreateSpaceResponse, CreateUserWorkspaceResponse, ListUserSpaceResponse, LoginRequest, RegisterRequest, RegisterResponse, TokenResponse
from custom.services import TOKEN_TTL, _hash_password,_create_access_token,_verify_password


router = APIRouter(prefix="/auth", tags=["auth"])
logger = structlog.getLogger(__name__)


@router.post("/register", response_model=RegisterResponse)
async def register(
    body: RegisterRequest, session=Depends(get_session)
) -> RegisterResponse:
    repo = UserRepository(session)
    if await repo.exists(body.account):
        raise HTTPException(status_code=409, detail="账号已存在")
    hashed = _hash_password(body.password)
    user = await repo.create(body.account, hashed)
    return RegisterResponse(account=user.account)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, session=Depends(get_session)) -> TokenResponse:
    repo = UserRepository(session)
    user = await repo.get(body.account)
    # 账号不存在与密码错误返回同一提示，避免账号枚举
    if user is None or not _verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    return TokenResponse(
        access_token=_create_access_token(user.account), expires_in=TOKEN_TTL
    )

@router.get(path="/user/spaces",)
async def get_user_spaces(session:Annotated[AsyncSession, Depends(get_session)],user:Annotated[UserTables, Depends(get_user_by_token)]):
    repo = UserWorkspaceRepository(session=session)
    spaces = await repo.list_by_user(user.account)
    msg = f"用户有{len(spaces)}"
    return ListUserSpaceResponse(space=spaces,message=msg)

@router.post(path="/spaces/create")
async def create_spaces(body:CreateSpaceInput,session:Annotated[AsyncSession, Depends(get_session)],is_super:Annotated[bool, Depends(get_super_user)]):
    if not is_super:
        msg = "only super user can create MicroServe spaces"
        logger.info(msg)
        return CreateSpaceResponse(space_name="",message=msg)
    repo = MicroServiceSpaceRepository(session)
    spaces = await repo.create(body.space_name,body.path)
    return CreateSpaceResponse(space_name=spaces.name,message=f"{spaces.name} 已经建立")

@router.post(path="/spaces_users/create")
async def CreateUserWorkspace (space_name:str,session:Annotated[AsyncSession, Depends(get_session)],user:Annotated[UserTables, Depends(get_user_by_token)]):
    repo = UserWorkspaceRepository(session)
    if not user.is_super:
        msg = "only super can assign spaces to user"
        logger.info(msg)
    user_work_space = await repo.create(user.account,space_name)
    return CreateUserWorkspaceResponse(space_name=user_work_space.workspace_name,user_account=user.account,message=f"成功将{user_work_space.workspace_name}分配给{user.account}")