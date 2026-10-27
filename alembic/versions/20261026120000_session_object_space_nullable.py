"""session_object.space 改可空：md 也进台账，但 md 不属于任何空间（ADR-0017）。

md 的字节从 LangGraph Store 搬到对象存储后，两类文件共用 `session_object`：
xlsx 的 `space` 从文件名前缀解析，md 没有空间可言 → NULL。

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-10-26 12:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None

_OLD_COMMENT = "从文件名前缀解析出的空间名，只用于路由到子 agent 与展示，**不是**隔离键"
_NEW_COMMENT = "从文件名前缀解析出的空间名；md 没有空间，为 NULL。只用于路由与展示，**不是**隔离键"


def upgrade() -> None:
    op.alter_column(
        "session_object",
        "space",
        existing_type=sa.String(length=100),
        nullable=True,
        existing_comment=_OLD_COMMENT,
        comment=_NEW_COMMENT,
    )


def downgrade() -> None:
    # 加回 NOT NULL 之前得先给 md 行一个值（空串），否则约束加不上
    op.execute("UPDATE session_object SET space = '' WHERE space IS NULL")
    op.alter_column(
        "session_object",
        "space",
        existing_type=sa.String(length=100),
        nullable=False,
        existing_comment=_NEW_COMMENT,
        comment=_OLD_COMMENT,
    )
