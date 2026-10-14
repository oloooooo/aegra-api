from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from custom.models import Space, UserSpace, UserTables


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
        return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue]

    async def delete(self, account: str) -> bool:
        """删除账号，返回是否命中行。"""
        stmt = delete(UserTables).where(UserTables.account == account)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue]


class SpaceRepository:
    """Space 的增删改查，session 由调用方传入。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, name: str) -> Space | None:
        return await self.session.get(Space, name)

    async def exists(self, name: str) -> bool:
        stmt = select(Space.name).where(Space.name == name)
        return (await self.session.execute(stmt)).scalar() is not None

    async def list_all(self) -> list[Space]:
        stmt = select(Space).order_by(Space.name)
        return list((await self.session.execute(stmt)).scalars())

    async def create(self, name: str, path: str) -> Space:
        space = Space(name=name, path=path)
        self.session.add(space)
        await self.session.commit()
        await self.session.refresh(space)
        return space

    async def update_path(self, name: str, path: str) -> bool:
        """更新工作目录路径，返回是否命中行。"""
        stmt = update(Space).where(Space.name == name).values(path=path)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue]

    async def delete(self, name: str) -> bool:
        """删除空间，返回是否命中行。"""
        stmt = delete(Space).where(Space.name == name)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue]


class UserSpaceRepository:
    """UserSpace（用户-微服务空间关联）的增删改查，session 由调用方传入。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, account: str, space_name: str) -> UserSpace | None:
        return await self.session.get(UserSpace, (account, space_name))

    async def exists(self, account: str, space_name: str) -> bool:
        stmt = select(UserSpace.account).where(
            UserSpace.account == account,
            UserSpace.space_name == space_name,
        )
        return (await self.session.execute(stmt)).scalar() is not None

    async def list_by_user(self, account: str) -> list[str]:
        """该用户可访问的微服务空间名。"""
        stmt = (
            select(UserSpace.space_name)
            .where(UserSpace.account == account)
            .order_by(UserSpace.space_name)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_by_space(self, space_name: str) -> list[UserSpace]:
        stmt = (
            select(UserSpace)
            .where(UserSpace.space_name == space_name)
            .order_by(UserSpace.account)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def create(self, account: str, space_name: str) -> UserSpace:
        link = UserSpace(account=account, space_name=space_name)
        self.session.add(link)
        await self.session.commit()
        await self.session.refresh(link)
        return link

    async def delete(self, account: str, space_name: str) -> bool:
        """解除关联，返回是否命中行。"""
        stmt = delete(UserSpace).where(
            UserSpace.account == account,
            UserSpace.space_name == space_name,
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue]
