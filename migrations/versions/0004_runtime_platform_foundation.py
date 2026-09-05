"""Add durable bootstrap, image verification and scheduler leadership state."""

import sqlalchemy as sa
from alembic import op

revision = "0004_runtime_platform_foundation"
down_revision = "0003_runtime_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runtime_instances") as batch:
        batch.add_column(
            sa.Column(
                "bootstrap_version", sa.Integer(), nullable=False, server_default="1"
            )
        )
        batch.add_column(
            sa.Column("bootstrap_phase", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "bootstrap_completed_at", sa.DateTime(timezone=True), nullable=True
            )
        )
        batch.add_column(
            sa.Column("bootstrap_error_code", sa.String(length=64), nullable=True)
        )
        batch.add_column(sa.Column("bootstrap_error_message", sa.Text(), nullable=True))
    with op.batch_alter_table("runtime_heartbeats") as batch:
        batch.add_column(
            sa.Column(
                "details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
            )
        )
    op.create_table(
        "runtime_images",
        sa.Column("version", sa.String(length=120), primary_key=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "scheduler_leadership",
        sa.Column("name", sa.String(length=80), primary_key=True),
        sa.Column("holder_id", sa.String(length=120), nullable=True),
        sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_scheduler_leadership_lease_until", "scheduler_leadership", ["lease_until"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scheduler_leadership_lease_until", table_name="scheduler_leadership"
    )
    op.drop_table("scheduler_leadership")
    op.drop_table("runtime_images")
    with op.batch_alter_table("runtime_heartbeats") as batch:
        batch.drop_column("details")
    with op.batch_alter_table("runtime_instances") as batch:
        batch.drop_column("bootstrap_error_message")
        batch.drop_column("bootstrap_error_code")
        batch.drop_column("bootstrap_completed_at")
        batch.drop_column("bootstrap_phase")
        batch.drop_column("bootstrap_version")
