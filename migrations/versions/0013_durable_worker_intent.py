"""Persist worker intent independently from the isolated worker process."""
import sqlalchemy as sa
from alembic import op

revision = "0013_durable_worker_intent"
down_revision = "0012_schedule_pause"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "worker_status",
        sa.Column(
            "desired_worker_state",
            sa.String(length=32),
            nullable=False,
            server_default="DISABLED",
        ),
    )


def downgrade():
    op.drop_column("worker_status", "desired_worker_state")
