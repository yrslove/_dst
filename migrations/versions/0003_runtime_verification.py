"""Persist verification independently from container state."""

import sqlalchemy as sa
from alembic import op

revision = "0003_runtime_verification"
down_revision = "0002_view_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy generations require explicit verification, never an inferred backfill.
    op.add_column(
        "runtime_instances",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("runtime_instances") as batch:
        batch.drop_column("verified_at")
