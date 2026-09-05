"""Store the runtime bootstrap token using the existing encrypted-secret mechanism."""

import sqlalchemy as sa
from alembic import op

revision = "0005_runtime_bootstrap_token"
down_revision = "0004_runtime_platform_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runtime_instances") as batch:
        batch.add_column(sa.Column("runtime_token_enc", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runtime_instances") as batch:
        batch.drop_column("runtime_token_enc")
