"""async_task_delivery -> async_task_outbox（ADR-0010 重写）

旧表是“投递认领”：一行代表“谁有权投这一次结果”，靠 8s/15s 两个时间窗口协调。
新表是事务性发件箱：一行代表“这次结果还没投出去”，由 `finalize_run` 在子 run 转
终态的同一个事务里插入，状态机 pending → delivered 由单行条件 UPDATE 推进。

换表而不是改列：旧行的语义（claimed_at）与新表不可兼容，旧数据是 15 秒量级的
瞬时账本，直接丢弃没有业务损失。

Revision ID: c4e8a1b9d2f7
Revises: b7c1d2e3f4a5
Create Date: 2026-10-22 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "c4e8a1b9d2f7"
down_revision = "b7c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "async_task_outbox",
        sa.Column("task_id", sa.String(length=64), nullable=False, comment="异步任务 id（= 子线程 id）"),
        sa.Column("run_id", sa.String(length=64), nullable=False, comment="子 agent 本次执行的 run id"),
        sa.Column("parent_thread_id", sa.String(length=64), nullable=False, comment="主管线程 id"),
        sa.Column("agent_name", sa.String(length=64), nullable=True, comment="子 agent 名（子线程的 graph_id）"),
        sa.Column("terminal_status", sa.String(length=32), nullable=False, comment="子 run 的终态"),
        sa.Column("summary", sa.Text(), nullable=True, comment="结果摘要（写入时截断）"),
        sa.Column(
            "status", sa.String(length=16), server_default="pending", nullable=False, comment="pending / delivered / failed"
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False, comment="投递尝试次数"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True, comment="分发者租约"),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("task_id", "run_id"),
    )
    op.create_index(
        "ix_async_task_outbox_pending", "async_task_outbox", ["status", "next_attempt_at"], unique=False
    )
    op.create_index(
        "ix_async_task_outbox_parent", "async_task_outbox", ["parent_thread_id"], unique=False
    )

    op.drop_index(op.f("ix_async_task_delivery_parent_thread_id"), table_name="async_task_delivery")
    op.drop_table("async_task_delivery")


def downgrade() -> None:
    op.create_table(
        "async_task_delivery",
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("parent_thread_id", sa.String(length=64), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("task_id", "run_id"),
    )
    op.create_index(
        op.f("ix_async_task_delivery_parent_thread_id"),
        "async_task_delivery",
        ["parent_thread_id"],
        unique=False,
    )

    op.drop_index("ix_async_task_outbox_parent", table_name="async_task_outbox")
    op.drop_index("ix_async_task_outbox_pending", table_name="async_task_outbox")
    op.drop_table("async_task_outbox")
