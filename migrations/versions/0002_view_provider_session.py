"""Add opaque provider identifier to short-lived view sessions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0002_view_provider"
down_revision = "0001_control_plane"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {
        item["name"]
        for item in inspect(op.get_bind()).get_columns("runtime_view_sessions")
    }


def upgrade() -> None:
    # Early development snapshots of revision 0001 were created before this column.
    if "provider_session_id" not in _columns():
        op.add_column(
            "runtime_view_sessions",
            sa.Column("provider_session_id", sa.String(length=255), nullable=True),
        )


def downgrade() -> None:
    if "provider_session_id" in _columns():
        with op.batch_alter_table("runtime_view_sessions") as batch:
            batch.drop_column("provider_session_id")
