"""add user and workspace,also with thier connect table

Revision ID: 13aeb356cc6c
Revises: c4d5e6f7a8b9
Create Date: 2026-10-14 19:22:36.719036

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '13aeb356cc6c'
down_revision = 'c4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('micro_service_space',
    sa.Column('name', sa.String(length=100), nullable=False, comment='业务空间名称'),
    sa.Column('path', sa.String(length=512), nullable=False, comment='工作目录路径'),
    sa.PrimaryKeyConstraint('name')
    )
    op.create_index(op.f('ix_micro_service_space_name'), 'micro_service_space', ['name'], unique=True)
    op.create_table('user_tables',
    sa.Column('account', sa.String(length=50), nullable=False, comment='登录账号'),
    sa.Column('hashed_password', sa.String(length=100), nullable=False, comment='bcrypt 哈希'),
    sa.Column('is_super', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='超级用户，只能直接改库'),
    sa.PrimaryKeyConstraint('account')
    )
    op.create_index(op.f('ix_user_tables_account'), 'user_tables', ['account'], unique=True)
    op.create_table('user_microservice',
    sa.Column('account', sa.String(length=50), nullable=False),
    sa.Column('workspace_name', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['account'], ['user_tables.account'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_name'], ['micro_service_space.name'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('account', 'workspace_name')
    )
    # NOTE: 原始 autogenerate 版本在这里夹带了 checkpoint_* / store* 的 drop_table，
    # 那些表由 langgraph 的 checkpointer/store 在启动时 .setup() 建立，不属于本迁移，
    # 全新库上执行会因表不存在而失败，已有库上执行会丢历史。已移除。


def downgrade() -> None:
    op.drop_table('user_microservice')
    op.drop_index(op.f('ix_user_tables_account'), table_name='user_tables')
    op.drop_table('user_tables')
    op.drop_index(op.f('ix_micro_service_space_name'), table_name='micro_service_space')
    op.drop_table('micro_service_space')
