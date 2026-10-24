from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
    func,
    text,
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
    org_id: Mapped[str | None] = mapped_column(
        String(50), comment="组织名", default=None
    )
    email: Mapped[str | None] = mapped_column(
        String(80), comment="邮箱名", default=None
    )

    hashed_password: Mapped[str] = mapped_column(String(100), comment="bcrypt 哈希")
    is_super: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), comment="超级用户，只能直接改库"
    )
    # 只读视图：带权限的关联走 UserSpace，写入请直接操作它
    spaces: Mapped[list[Space]] = relationship(secondary="user_space", viewonly=True)

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
    users: Mapped[list[UserTables]] = relationship(
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

class AsyncTaskOutbox(Base):
    """后台任务结果的发件箱（ADR-0010，Transactional Outbox）。

    一行 = 一次子 run 的终态结果**待投递**。写入点是 `finalize_run`（子 run 转终态的
    同一个事务里插入），主键 (task_id, run_id) 就是幂等键。

    状态机：`pending` →（分发者建唤醒 run）/（父 run 的 hook 写 state）→ `delivered`；
    重试耗尽 → `failed`。**没有任何时间窗口**：“谁先把行从 pending 翻成 delivered，
    谁才有资格投”是一个单行条件 UPDATE，本身就是原子的。
    """

    __tablename__ = "async_task_outbox"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="异步任务 id（= 子线程 id）")
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="子 agent 本次执行的 run id")
    parent_thread_id: Mapped[str] = mapped_column(String(64), comment="主管线程 id")
    agent_name: Mapped[str | None] = mapped_column(String(64), comment="子 agent 名（子线程的 graph_id）")
    terminal_status: Mapped[str] = mapped_column(String(32), comment="子 run 的终态：success / error / timeout")

    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", comment="pending / delivered / failed"
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), comment="投递尝试次数")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="最早何时重试（退避）"
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
        comment="分发者租约：拿批时写，崩溃后过期即可被别人接手",
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # 分发者的领取查询：status + next_attempt_at 过滤，按时间排序
        Index("ix_async_task_outbox_pending", "status", "next_attempt_at"),
        Index("ix_async_task_outbox_parent", "parent_thread_id"),
    )

    def __repr__(self) -> str:
        return f"<AsyncTaskOutbox {self.task_id!r}/{self.run_id!r} {self.status} attempts={self.attempts}>"
