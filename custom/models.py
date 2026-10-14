from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    String,
    false,
)
from sqlalchemy.orm import Mapped, declarative_base, mapped_column, relationship

from aegra_api.core.orm import Base


# Base = declarative_base()
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
    # 只读视图：带权限的关联走 UserWorkspace，写入请直接操作它
    workspaces: Mapped[list["MicroServiceSpace"]] = relationship(
        secondary="user_microservice", viewonly=True
    )

    def __repr__(self) -> str:
        return f"<User {self.account!r}>"


class MicroServiceSpace(Base):
    """微服务空间：一个独立的微服务空间，name 唯一，path 指向磁盘或者记忆系统目录。"""

    __tablename__ = "micro_service_space"

    name: Mapped[str] = mapped_column(
        String(100), index=True, unique=True, comment="业务空间名称", primary_key=True
    )
    path: Mapped[str] = mapped_column(String(512), comment="工作目录路径")
    # 只读视图，写入走 UserWorkspace
    users: Mapped[list["UserTables"]] = relationship(
        secondary="user_microservice", viewonly=True
    )

    def __repr__(self) -> str:
        return f"<Workspace {self.name!r}>"


class UserWorkspace(Base):
    """用户 <-> 微服务空间 多对多关联，写入直接操作本表。"""

    __tablename__ = "user_microservice"

    account: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("user_tables.account", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_name: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("micro_service_space.name", ondelete="CASCADE"),
        primary_key=True,
    )

    def __repr__(self) -> str:
        return f"<UserWorkspace {self.account!r} -> {self.workspace_name!r}>"