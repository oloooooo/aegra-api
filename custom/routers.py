"""注册/登录路由：bcrypt 加盐哈希 + pepper 存储密码，登录签发 JWT access token。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
import structlog
from aegra_api.models.auth import User
from custom.models import UserTables
from custom.repositories import SpaceRepository, UserRepository, UserSpaceRepository
from custom.dependencies import get_session
from aegra_api.core.auth_deps import require_auth
from custom.schemas import (
    CreateSpaceInput,
    CreateSpaceResponse,
    CreateUserSpaceResponse,
    ListUserSpaceResponse,
    LoginRequest,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
)
from custom.services import (
    TOKEN_TTL,
    _hash_password,
    _create_access_token,
    _verify_password,
)

router = APIRouter(prefix="/custom", tags=["/custom"])
logger = structlog.getLogger(__name__)


@router.post("/register", response_model=RegisterResponse)
async def register(
    body: RegisterRequest, session=Depends(get_session)
) -> RegisterResponse:
    repo = UserRepository(session)
    if await repo.exists(body.identity):
        raise HTTPException(status_code=409, detail="账号名已存在")
    hashed = _hash_password(body.password)
    user = await repo.create(
        identity=body.identity,
        display_name=body.display_name,
        hashed_password=hashed,
        email=body.email,
        org_id=body.org_id,
    )
    return RegisterResponse(user_identity=user.identity)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, session=Depends(get_session)) -> TokenResponse:
    repo = UserRepository(session)
    user = await repo.get(body.identity)
    # 账号不存在与密码错误返回同一提示，避免账号枚举
    if user is None or not _verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    return TokenResponse(
        access_token=_create_access_token(user.identity), expires_in=TOKEN_TTL
    )


@router.get("/whoami")
async def whoami(user: User = Depends(require_auth)):
    return {
        "identity": user.identity,
        "display_name": user.display_name,
        "permissions": user.permissions,
    }


@router.get("/user/spaces", response_model=ListUserSpaceResponse)
async def get_user_spaces(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> ListUserSpaceResponse:
    repo = UserSpaceRepository(session=session)
    spaces = await repo.list_by_user(user.identity)
    return ListUserSpaceResponse(space=spaces, message=f"用户有{len(spaces)}")


@router.post("/spaces/create", response_model=CreateSpaceResponse)
async def create_space(
    body: CreateSpaceInput,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> CreateSpaceResponse:
    if "write" not in user.permissions:
        raise HTTPException(status_code=403, detail="只有超级用户可以创建微服务空间")
    repo = SpaceRepository(session)
    space = await repo.create(body.space_name, body.path)
    return CreateSpaceResponse(space_name=space.name, message=f"{space.name} 已经建立")


@router.post("/spaces/users/create", response_model=CreateUserSpaceResponse)
async def create_user_space(
    identity: str,
    space_name: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> CreateUserSpaceResponse:
    if "write" not in user.permissions:
        raise HTTPException(status_code=403, detail="只有超级用户可以分配微服务空间")
    repo = UserSpaceRepository(session)
    link = await repo.create(identity, space_name)
    return CreateUserSpaceResponse(
        space_name=link.space_name,
        user_identity=link.identity,
        message=f"成功将{link.space_name}分配给{link.identity}",
    )