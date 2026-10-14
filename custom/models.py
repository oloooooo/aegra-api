from __future__ import annotations

from sqlalchemy import (
    Boolean,
    ForeignKey,
    String,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aegra_api.core.orm import Base


class UserTables(Base):
    """账号 / 密码哈希"""

    __tablename__ = "user_tables"

    account: Mapped[str] = mapped_column(
        String(50), index=True, unique=True, comment="登录账号", primary_key=True
    )

    hashed_password: Mapped[str] = mapped_column(String(100), comment="bcrypt 哈希")
    is_super: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), comment="超级用户，只能直接改库"
    )
    # 只读视图：带权限的关联走 UserSpace，写入请直接操作它
    spaces: Mapped[list["Space"]] = relationship(
        secondary="user_space", viewonly=True
    )

    def __repr__(self) -> str:
        return f"<User {self.account!r}>"


class Space(Base):
    """微服务空间：知识空间（KNOWLEDGE_SPACE_DIR）下的一个文件夹。"""

    __tablename__ = "space"

    name: Mapped[str] = mapped_column(
        String(100),
        index=True,
        unique=True,
        comment="微服务空间名称，即知识空间下的文件夹名",
        primary_key=True,
    )
    path: Mapped[str] = mapped_column(
        String(512), comment="工作目录路径，相对项目根（服务启动时的 CWD）"
    )
    # 只读视图，写入走 UserSpace
    users: Mapped[list["UserTables"]] = relationship(
        secondary="user_space", viewonly=True
    )

    def __repr__(self) -> str:
        return f"<Space {self.name!r}>"


class UserSpace(Base):
    """用户 <-> 微服务空间 多对多关联，写入直接操作本表。"""

    __tablename__ = "user_space"

    account: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("user_tables.account", ondelete="CASCADE"),
        primary_key=True,
    )
    space_name: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("space.name", ondelete="CASCADE"),
        primary_key=True,
    )

    def __repr__(self) -> str:
        return f"<UserSpace {self.account!r} -> {self.space_name!r}>"
