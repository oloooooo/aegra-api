"""session_object：会话文件里二进制的元数据（ADR-0015）。

字节在对象存储，行在这里；主键 (上传者, 主管 thread, 文件名)，同名覆盖 = PUT。
删会话时按 (owner_identity, thread_id) 删行 + 按前缀删对象（`custom/blobs.py`）。

Revision ID: c1d2e3f4a5b6
Revises: e5f6a7b8c9d0
Create Date: 2026-10-25 13:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "c1d2e3f4a5b6"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_object",
        sa.Column(
            "owner_identity",
            sa.String(length=50),
            nullable=False,
            comment="上传者身份；与 thread_id 一起构成私有隔离前缀",
        ),
        sa.Column("thread_id", sa.String(length=64), nullable=False, comment="主管线程 id（= 会话），删会话即删"),
        sa.Column("name", sa.String(length=200), nullable=False, comment="上传文件名，形如 <空间名>_<表名>.xlsx"),
        sa.Column(
            "space",
            sa.String(length=100),
            nullable=False,
            comment="从文件名前缀解析出的空间名，只用于路由到子 agent 与展示，**不是**隔离键",
        ),
        sa.Column("object_key", sa.String(length=512), nullable=False, comment="对象存储 key（含 {sha16}）"),
        sa.Column("sha256", sa.String(length=64), nullable=False, comment="内容哈希；表索引的缓存键"),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, comment="字节数；配额按它累加"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_identity"], ["user_tables.identity"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("owner_identity", "thread_id", "name"),
    )
    op.create_index("ix_session_object_owner", "session_object", ["owner_identity"], unique=False)
    op.create_index("ix_session_object_thread", "session_object", ["thread_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_session_object_thread", table_name="session_object")
    op.drop_index("ix_session_object_owner", table_name="session_object")
    op.drop_table("session_object")
