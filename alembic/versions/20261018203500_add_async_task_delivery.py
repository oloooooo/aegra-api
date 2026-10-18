"""add async_task_delivery

后台任务结果投递的认领表（ADR-0010 修复跨进程重复投递 / 并发投递 run）：
主键 (task_id, run_id) 即判重键，parent_thread_id 用于同一父线程上的并发让路。

Revision ID: b7c1d2e3f4a5
Revises: 38e716308967
Create Date: 2026-10-18 20:35:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b7c1d2e3f4a5'
down_revision = '38e716308967'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'async_task_delivery',
        sa.Column('task_id', sa.String(length=64), nullable=False, comment='异步任务 id（= 子线程 id）'),
        sa.Column('run_id', sa.String(length=64), nullable=False, comment='子 agent 本次执行的 run id'),
        sa.Column('parent_thread_id', sa.String(length=64), nullable=False, comment='主管线程 id'),
        sa.Column(
            'claimed_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
            comment='认领时间，长于阈值视为过期可重投',
        ),
        sa.PrimaryKeyConstraint('task_id', 'run_id'),
    )
    op.create_index(
        op.f('ix_async_task_delivery_parent_thread_id'),
        'async_task_delivery',
        ['parent_thread_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_async_task_delivery_parent_thread_id'), table_name='async_task_delivery')
    op.drop_table('async_task_delivery')
