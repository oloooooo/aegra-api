from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from custom.models import MicroServiceSpace, UserTables, UserWorkspace



class UserRepository:
    """UserTables 的增删改查，session 由调用方传入。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, account: str) -> UserTables | None:
        return await self.session.get(UserTables, account)

    async def exists(self, account: str) -> bool:
        stmt = select(UserTables.account).where(UserTables.account == account)
        return (await self.session.execute(stmt)).scalar() is not None

    async def create(self, account: str, hashed_password: str) -> UserTables:
        user = UserTables(account=account, hashed_password=hashed_password)
        self.session.add(user)
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def update_password(self, account: str, hashed_password: str) -> bool:
        """更新密码哈希，返回是否命中行。"""
        stmt = (
            update(UserTables)
            .where(UserTables.account == account)
            .values(hashed_password=hashed_password)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0 # pyright: ignore[reportAttributeAccessIssue]

    async def delete(self, account: str) -> bool:
        """删除账号，返回是否命中行。"""
        stmt = delete(UserTables).where(UserTables.account == account)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0 # pyright: ignore[reportAttributeAccessIssue]


class MicroServiceSpaceRepository:
    """MicroServiceSpace 的增删改查，session 由调用方传入。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, name: str) -> MicroServiceSpace | None:
        return await self.session.get(MicroServiceSpace, name)

    async def exists(self, name: str) -> bool:
        stmt = select(MicroServiceSpace.name).where(MicroServiceSpace.name == name)
        return (await self.session.execute(stmt)).scalar() is not None

    async def list_all(self) -> list[MicroServiceSpace]:
        stmt = select(MicroServiceSpace).order_by(MicroServiceSpace.name)
        return list((await self.session.execute(stmt)).scalars())

    async def create(self, name: str, path: str) -> MicroServiceSpace:
        space = MicroServiceSpace(name=name, path=path)
        self.session.add(space)
        await self.session.commit()
        await self.session.refresh(space)
        return space

    async def update_path(self, name: str, path: str) -> bool:
        """更新工作目录路径，返回是否命中行。"""
        stmt = (
            update(MicroServiceSpace)
            .where(MicroServiceSpace.name == name)
            .values(path=path)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue]

    async def delete(self, name: str) -> bool:
        """删除空间，返回是否命中行。"""
        stmt = delete(MicroServiceSpace).where(MicroServiceSpace.name == name)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0 # pyright: ignore[reportAttributeAccessIssue]


class UserWorkspaceRepository:
    """UserWorkspace（用户-空间关联）的增删改查，session 由调用方传入。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, account: str, workspace_name: str) -> UserWorkspace | None:
        return await self.session.get(UserWorkspace, (account, workspace_name))

    async def exists(self, account: str, workspace_name: str) -> bool:
        stmt = select(UserWorkspace.account).where(
            UserWorkspace.account == account,
            UserWorkspace.workspace_name == workspace_name,
        )
        return (await self.session.execute(stmt)).scalar() is not None

    async def list_by_user(self, account: str) -> list[str]:
        stmt = (
            select(UserWorkspace)
            .where(UserWorkspace.account == account)
            .order_by(UserWorkspace.workspace_name)
        )
        return list((await self.session.execute(stmt)).scalars().__dict__)

    async def list_by_workspace(self, workspace_name: str) -> list[UserWorkspace]:
        stmt = (
            select(UserWorkspace)
            .where(UserWorkspace.workspace_name == workspace_name)
            .order_by(UserWorkspace.account)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def create(self, account: str, workspace_name: str) -> UserWorkspace:
        link = UserWorkspace(account=account, workspace_name=workspace_name)
        self.session.add(link)
        await self.session.commit()
        await self.session.refresh(link)
        return link

    async def delete(self, account: str, workspace_name: str) -> bool:
        """解除关联，返回是否命中行。"""
        stmt = delete(UserWorkspace).where(
            UserWorkspace.account == account,
            UserWorkspace.workspace_name == workspace_name,
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0 # pyright: ignore[reportAttributeAccessIssue]