"""update user

user_tables.account -> identity，并新增 display_name/org_id/email；
user_space.account -> identity（外键约束随列改名自动跟随，这里只改约束名）。
列是改名而非重建，保留既有数据。

Revision ID: 38e716308967
Revises: b4c1d2e3f4a5
Create Date: 2026-10-17 10:03:45.374648

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '38e716308967'
down_revision = 'b4c1d2e3f4a5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # user_tables: account -> identity
    op.execute('ALTER TABLE user_tables RENAME COLUMN account TO identity')
    op.execute('ALTER INDEX ix_user_tables_account RENAME TO ix_user_tables_identity')

    op.add_column('user_tables', sa.Column('display_name', sa.String(length=10), nullable=True, comment='用户名'))
    # 已有行用 identity 前 10 位回填，再收紧 NOT NULL
    op.execute("UPDATE user_tables SET display_name = left(identity, 10) WHERE display_name IS NULL")
    op.alter_column('user_tables', 'display_name', nullable=False)
    op.create_index(op.f('ix_user_tables_display_name'), 'user_tables', ['display_name'], unique=False)

    op.add_column('user_tables', sa.Column('org_id', sa.String(length=50), nullable=True, comment='组织名'))
    op.add_column('user_tables', sa.Column('email', sa.String(length=80), nullable=True, comment='邮箱名'))

    # user_space: account -> identity，约束名同步
    op.execute('ALTER TABLE user_space RENAME COLUMN account TO identity')
    op.execute('ALTER TABLE user_space RENAME CONSTRAINT user_space_account_fkey TO user_space_identity_fkey')

    op.alter_column('space', 'name',
               existing_type=sa.VARCHAR(length=100),
               comment='微服务空间名称，即知识空间下的文件夹名',
               existing_comment='业务空间名称',
               existing_nullable=False)
    op.alter_column('space', 'path',
               existing_type=sa.VARCHAR(length=512),
               comment='工作目录路径，相对项目根（服务启动时的 CWD）',
               existing_comment='工作目录路径',
               existing_nullable=False)
    # NOTE: 原始 autogenerate 版本在这里夹带了 checkpoint_* / store* 的 drop_table，
    # 那些表由 langgraph 的 checkpointer/store 在启动时 .setup() 建立，不属于本迁移，
    # 全新库上执行会因表不存在而失败，已有库上执行会丢历史。已移除。


def downgrade() -> None:
    op.alter_column('space', 'path',
               existing_type=sa.VARCHAR(length=512),
               comment='工作目录路径',
               existing_comment='工作目录路径，相对项目根（服务启动时的 CWD）',
               existing_nullable=False)
    op.alter_column('space', 'name',
               existing_type=sa.VARCHAR(length=100),
               comment='业务空间名称',
               existing_comment='微服务空间名称，即知识空间下的文件夹名',
               existing_nullable=False)

    op.execute('ALTER TABLE user_space RENAME CONSTRAINT user_space_identity_fkey TO user_space_account_fkey')
    op.execute('ALTER TABLE user_space RENAME COLUMN identity TO account')

    op.drop_column('user_tables', 'email')
    op.drop_column('user_tables', 'org_id')
    op.drop_index(op.f('ix_user_tables_display_name'), table_name='user_tables')
    op.drop_column('user_tables', 'display_name')
    op.execute('ALTER INDEX ix_user_tables_identity RENAME TO ix_user_tables_account')
    op.execute('ALTER TABLE user_tables RENAME COLUMN identity TO account')
