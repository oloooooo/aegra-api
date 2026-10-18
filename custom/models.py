from __future__ import annotations
from typing import Optional

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aegra_api.core.orm import Base


class UserTables(Base):
    """账号 / 密码哈希"""

    __tablename__ = "user_tables"

    identity: Mapped[str] = mapped_column(
        String(50), index=True, unique=True, comment="登录账号", primary_key=True
    )
    display_name: Mapped[str] = mapped_column(
        String(10),
        index=True,
        comment="用户名",
    )
    org_id: Mapped[Optional[str]] = mapped_column(
        String(50), comment="组织名", default=None
    )
    email: Mapped[Optional[str]] = mapped_column(
        String(80), comment="邮箱名", default=None
    )

    hashed_password: Mapped[str] = mapped_column(String(100), comment="bcrypt 哈希")
    is_super: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), comment="超级用户，只能直接改库"
    )
    # 只读视图：带权限的关联走 UserSpace，写入请直接操作它
    spaces: Mapped[list["Space"]] = relationship(secondary="user_space", viewonly=True)

    def __repr__(self) -> str:
        return f"<User {self.identity!r}>"


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

    identity: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("user_tables.identity", ondelete="CASCADE"),
        primary_key=True,
    )
    space_name: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("space.name", ondelete="CASCADE"),
        primary_key=True,
    )

    def __repr__(self) -> str:
        return f"<UserSpace {self.identity!r} -> {self.space_name!r}>"

class AsyncTaskDelivery(Base):
    """后台任务结果的投递认领：跨进程/跨 run 的幂等键。

    一行 = (task_id, run_id) 这次执行的结果**已经有权**投递给主管。主键即判重键：
    `INSERT ... ON CONFLICT DO NOTHING` 抢到的那个调用方才有资格开投递 run 或注入消息。
    `parent_thread_id` 用于同一父线程上的并发投递让路（短租约看 `claimed_at`）。
    """

    __tablename__ = "async_task_delivery"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="异步任务 id（= 子线程 id）")
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="子 agent 本次执行的 run id")
    parent_thread_id: Mapped[str] = mapped_column(String(64), index=True, comment="主管线程 id")
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="认领时间，长于阈值视为过期可重投"
    )

    def __repr__(self) -> str:
        return f"<AsyncTaskDelivery {self.task_id!r}/{self.run_id!r}>"
