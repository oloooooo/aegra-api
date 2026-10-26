"""知识空间（Space）：建空间、分配成员、浏览目录与文件树。

空间是权限的最小单位（CONTEXT.md「用户与空间词表」）。目录由空间名在知识空间根下派生，
所以「建空间」= 建目录 + ``AGENT.md``（``shared.knowledge.spec.scaffold_space``）；
增删空间不改代码。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.auth_deps import require_auth
from aegra_api.core.orm import get_session
from aegra_api.models.auth import User
from custom.api.schemas import (
    CreateSpaceInput,
    CreateSpaceResponse,
    CreateUserSpaceResponse,
    ListUserSpaceResponse,
)
from custom.db.repositories import SpaceRepository, UserSpaceRepository
from shared.knowledge.spec import scaffold_space, space_dir, space_rel_path, valid_space_name

logger = structlog.getLogger(__name__)

router = APIRouter(prefix="/custom", tags=["/custom"])



def _count_files(root: Path) -> int:
    """空间内文件数（同步遍历）。调用方必须用 `asyncio.to_thread`：`rglob` 是阻塞 IO，
    直接在 `async def` 里跑会卡住事件循环（space 一大，每次打开空间页都卡）。"""
    return sum(1 for p in root.rglob("*") if p.is_file()) if root.is_dir() else 0


def _list_files(root: Path) -> list[dict[str, Any]]:
    """空间文件树（同步遍历 + stat）。同上，必须经 `asyncio.to_thread` 调用。"""
    out: list[dict[str, Any]] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue  # 遍历期间被删除/改名：跳过，不要让整个文件列表 500
        out.append(
            {
                "path": "/" + p.relative_to(root).as_posix(),
                "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
                "ext": p.suffix.lower(),
            }
        )
    return out


async def _own_space_names(session: AsyncSession, user: User) -> set[str]:
    if "write" in (user.permissions or []):
        return {s.name for s in await SpaceRepository(session).list_all()}
    return set(await UserSpaceRepository(session).list_by_user(user.identity))


@router.get("/user/spaces", response_model=ListUserSpaceResponse)
async def get_user_spaces(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> ListUserSpaceResponse:
    repo = UserSpaceRepository(session=session)
    spaces = await repo.list_by_user(user.identity)
    return ListUserSpaceResponse(space=spaces, message=f"用户有{len(spaces)}")


@router.get("/spaces")
async def list_spaces(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> dict[str, Any]:
    """全部空间 + 当前用户是否有权限 + 文件数。"""
    spaces = await SpaceRepository(session).list_all()
    mine = set(await UserSpaceRepository(session).list_by_user(user.identity))
    is_super = "write" in (user.permissions or [])
    out = []
    for s in spaces:
        # 目录以空间名派生（shared.knowledge.spec.space_dir），不读 space.path：
        # 数据目录搬过家（graphs/okf -> knowledge），存量行里的 path 是旧值。
        files = await asyncio.to_thread(_count_files, space_dir(s.name))
        out.append(
            {
                "name": s.name,
                "path": s.path,
                "accessible": is_super or s.name in mine,
                "file_count": files,
            }
        )
    return {"spaces": out, "is_super": is_super}


@router.get("/spaces/{space_name}/files")
async def list_space_files(
    space_name: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> dict[str, Any]:
    """空间内文件树（只读）。递归列出相对路径、大小、修改时间。"""
    if space_name not in await _own_space_names(session, user):
        raise HTTPException(status_code=403, detail=f"无权访问空间 {space_name}")
    space = await SpaceRepository(session).get(space_name)
    if space is None:
        raise HTTPException(status_code=404, detail="空间不存在")
    root = space_dir(space_name)
    rel = space_rel_path(space_name)
    if not root.is_dir():
        return {"space": space_name, "root": rel, "files": []}
    return {"space": space_name, "root": rel, "files": await asyncio.to_thread(_list_files, root)}


@router.get("/spaces/{space_name}/users")
async def list_space_users(
    space_name: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> dict[str, Any]:
    """空间成员（超级用户可看全部，普通用户只看自己所在空间）。"""
    if space_name not in await _own_space_names(session, user):
        raise HTTPException(status_code=403, detail=f"无权访问空间 {space_name}")
    rows = await UserSpaceRepository(session).list_by_space(space_name)
    return {"space": space_name, "users": [r.identity for r in rows]}


@router.post("/spaces/create", response_model=CreateSpaceResponse)
async def create_space(
    body: CreateSpaceInput,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> CreateSpaceResponse:
    """建一个空间 = 建一个子 agent：目录 + AGENT.md 由知识空间根派生，不收客户端路径。

    每个空间就是一个领域子 agent（见 shared/knowledge/spec.py）：名字同时是目录名与
    子 agent 名，所以只校验一处。目录里没有 AGENT.md 时用模板生成，字段缺省也有兜底。
    """
    if "write" not in user.permissions:
        raise HTTPException(status_code=403, detail="只有超级用户可以创建微服务空间")
    name = body.space_name.strip()
    if not valid_space_name(name):
        raise HTTPException(status_code=422, detail="空间名只能用字母/数字/下划线/点/短横线，1-64 字符")
    repo = SpaceRepository(session)
    # path 由名字派生（知识空间根/名字）；body.path 只为老客户端兼容，不参与访问控制
    space = await repo.create(name, space_rel_path(name))
    scaffold_space(name)
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


@router.delete("/spaces/{space_name}/users/{identity}")
async def remove_space_user(
    space_name: str,
    identity: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(require_auth)],
) -> dict[str, Any]:
    """把用户移出空间（仅超级用户）。幂等：本来就不在也算成功（existed=false）。"""
    if "write" not in (user.permissions or []):
        raise HTTPException(status_code=403, detail="只有超级用户可以分配空间")
    existed = await UserSpaceRepository(session).delete(identity, space_name)
    return {"space": space_name, "removed": identity, "existed": existed}
