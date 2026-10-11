"""add runs.interrupted_by

Revision ID: c4d5e6f7a8b9
Revises: a3f7c1d9e2b4
Create Date: 2026-10-11 00:00:00.000000

Attributes the terminal `interrupted` status to its cause: 'cancel' (client
cancel or Supersede) or 'hitl' (graph interrupt). NULL for all other runs.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "c4d5e6f7a8b9"
down_revision = "a3f7c1d9e2b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("interrupted_by", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "interrupted_by")
