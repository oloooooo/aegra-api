"""对齐 async_task_outbox 列注释与 ORM（c4e8a1b9d2f7 的 4 条注释已改，但真库按旧版跑过）。

Revision ID: d1e2f3a4b5c6
Revises: c4e8a1b9d2f7
Create Date: 2026-10-22 13:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "d1e2f3a4b5c6"
down_revision = "c4e8a1b9d2f7"
branch_labels = None
depends_on = None

# 只改注释，与 ORM custom/models.py AsyncTaskOutbox 保持一致（c4e8a1b9d2f7 已同步改）
_COMMENTS = {
    "terminal_status": "子 run 的终态：success / error / timeout",
    "summary": "结果摘要（写入时截断，全文走 check_async_task）",
    "next_attempt_at": "最早何时重试（退避）",
    "lease_expires_at": "分发者租约：拿批时写，崩溃后过期即可被别人接手",
}


# c4e8a1b9d2f7 落库时的旧注释
_OLD_COMMENTS = {
    "terminal_status": "子 run 的终态",
    "summary": "结果摘要（写入时截断）",
    "next_attempt_at": None,
    "lease_expires_at": "分发者租约",
}


def upgrade() -> None:
    for column, comment in _COMMENTS.items():
        op.execute(f'COMMENT ON COLUMN async_task_outbox.{column} IS \'{comment}\'')


def downgrade() -> None:
    for column, comment in _OLD_COMMENTS.items():
        value = "NULL" if comment is None else f"'{comment}'"
        op.execute(f"COMMENT ON COLUMN async_task_outbox.{column} IS {value}")
