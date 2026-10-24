"""去掉 async_task_outbox.summary：它恒为 "(no output)"，没人读得出信息。

子 run 只声明 `stream_mode=["messages","custom"]`，不产生 `values` 事件，
`finalize_run` 拿到的 `output` 是空字典，所以这一列存进去的永远是 "(no output)"。
投递正文本来就走“现查子线程”（`_child_summary` / `_summary_async`），现查还能拿到
子 agent 在 run 里后追加的内容，比写入时刻的快照更准。所以删列不影响行为：
分发者在现查失败时改为直接返回 "(no output)"，和从这一列读到的是同一个字符串。

Revision ID: e5f6a7b8c9d0
Revises: d1e2f3a4b5c6
Create Date: 2026-10-24 11:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e5f6a7b8c9d0"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("async_task_outbox", "summary")


def downgrade() -> None:
    # 历史值找不回来（都是 "(no output)"），只能把列加回去
    op.add_column(
        "async_task_outbox",
        sa.Column("summary", sa.Text(), nullable=True, comment="结果摘要（写入时截断）"),
    )
