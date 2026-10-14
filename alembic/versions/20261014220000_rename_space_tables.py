"""rename micro_service_space/user_microservice to space/user_space

统一术语：微服务空间 → Space（表 space，关联表 user_space，外键列 space_name）。
旧迁移 13aeb356cc6c 已在库里执行过，故用改名而非重建。

Revision ID: b4c1d2e3f4a5
Revises: 13aeb356cc6c
Create Date: 2026-10-14 22:00:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'b4c1d2e3f4a5'
down_revision = '13aeb356cc6c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table('micro_service_space', 'space')
    op.rename_table('user_microservice', 'user_space')
    op.execute('ALTER TABLE user_space RENAME COLUMN workspace_name TO space_name')

    op.execute('ALTER INDEX ix_micro_service_space_name RENAME TO ix_space_name')
    op.execute('ALTER TABLE space RENAME CONSTRAINT micro_service_space_pkey TO space_pkey')
    op.execute('ALTER TABLE space RENAME CONSTRAINT micro_service_space_name_not_null TO space_name_not_null')
    op.execute('ALTER TABLE space RENAME CONSTRAINT micro_service_space_path_not_null TO space_path_not_null')

    op.execute('ALTER TABLE user_space RENAME CONSTRAINT user_microservice_pkey TO user_space_pkey')
    op.execute('ALTER TABLE user_space RENAME CONSTRAINT user_microservice_account_fkey TO user_space_account_fkey')
    op.execute(
        'ALTER TABLE user_space RENAME CONSTRAINT user_microservice_workspace_name_fkey TO user_space_space_name_fkey'
    )
    op.execute(
        'ALTER TABLE user_space RENAME CONSTRAINT user_microservice_account_not_null TO user_space_account_not_null'
    )
    op.execute(
        'ALTER TABLE user_space RENAME CONSTRAINT user_microservice_workspace_name_not_null'
        ' TO user_space_space_name_not_null'
    )


def downgrade() -> None:
    op.execute('ALTER TABLE user_space RENAME CONSTRAINT user_space_space_name_not_null'
               ' TO user_microservice_workspace_name_not_null')
    op.execute('ALTER TABLE user_space RENAME CONSTRAINT user_space_account_not_null'
               ' TO user_microservice_account_not_null')
    op.execute('ALTER TABLE user_space RENAME CONSTRAINT user_space_space_name_fkey'
               ' TO user_microservice_workspace_name_fkey')
    op.execute('ALTER TABLE user_space RENAME CONSTRAINT user_space_account_fkey'
               ' TO user_microservice_account_fkey')
    op.execute('ALTER TABLE user_space RENAME CONSTRAINT user_space_pkey TO user_microservice_pkey')

    op.execute('ALTER TABLE space RENAME CONSTRAINT space_path_not_null TO micro_service_space_path_not_null')
    op.execute('ALTER TABLE space RENAME CONSTRAINT space_name_not_null TO micro_service_space_name_not_null')
    op.execute('ALTER TABLE space RENAME CONSTRAINT space_pkey TO micro_service_space_pkey')
    op.execute('ALTER INDEX ix_space_name RENAME TO ix_micro_service_space_name')

    op.execute('ALTER TABLE user_space RENAME COLUMN space_name TO workspace_name')
    op.rename_table('user_space', 'user_microservice')
    op.rename_table('space', 'micro_service_space')
