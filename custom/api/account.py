"""注册 / 登录 / whoami：bcrypt+pepper 存密码，登录签发 JWT access token。"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError

from aegra_api.core.auth_deps import require_auth
from aegra_api.models.auth import User
from custom.api.deps import get_session
from custom.api.schemas import LoginRequest, RegisterRequest, RegisterResponse, TokenResponse
from custom.db.repositories import UserRepository
from custom.service.account import _hash_password, _verify_password
from shared.token import TOKEN_TTL, create_access_token

logger = structlog.getLogger(__name__)

router = APIRouter(prefix="/custom", tags=["/custom"])


@router.post("/register", response_model=RegisterResponse)
async def register(
    body: RegisterRequest, session=Depends(get_session)
) -> RegisterResponse:
    repo = UserRepository(session)
    if await repo.exists(body.identity):
        raise HTTPException(status_code=409, detail="账号名已存在")
    hashed = _hash_password(body.password)
    try:
        user = await repo.create(
            identity=body.identity,
            display_name=body.display_name,
            hashed_password=hashed,
            email=body.email,
            org_id=body.org_id,
        )
    except IntegrityError:
        # exists() 与 create() 之间是 TOCTOU：并发注册同名会撞主键，归一成 409 而不是 500
        await session.rollback()
        raise HTTPException(status_code=409, detail="账号名已存在") from None
    return RegisterResponse(user_identity=user.identity)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, session=Depends(get_session)) -> TokenResponse:
    repo = UserRepository(session)
    user = await repo.get(body.identity)
    # 账号不存在与密码错误返回同一提示，避免账号枚举
    if user is None or not _verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    return TokenResponse(
        access_token=create_access_token(user.identity), expires_in=TOKEN_TTL
    )


@router.get("/whoami")
async def whoami(user: User = Depends(require_auth)):
    return {
        "identity": user.identity,
        "display_name": user.display_name,
        "permissions": user.permissions,
    }
