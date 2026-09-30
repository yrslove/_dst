"""Persist account reward cadence inputs and active intent reservation."""

import sqlalchemy as sa
from alembic import op

revision = "0011_account_schedule_state"
down_revision = "0010_long_session"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_schedule_states",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("daily_status", sa.String(length=16), nullable=False),
        sa.Column("weekly_collected", sa.Integer(), nullable=False),
        sa.Column("weekly_target", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=24), nullable=False),
        sa.Column("pending_gift", sa.Boolean(), nullable=False),
        sa.Column("next_daily_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_weekly_eligible_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_daily_claim_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_weekly_claim_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_time_to_gift_seconds", sa.Float(), nullable=True),
        sa.Column("schedule_revision", sa.Integer(), nullable=False),
        sa.Column("active_job_key", sa.String(length=160), nullable=True),
        sa.Column("active_job_type", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "daily_status IN ('PENDING','DONE')", name="ck_schedule_daily_status"
        ),
        sa.CheckConstraint(
            "phase IN ('FARMING','FINAL_COLLECTION','DONE')", name="ck_schedule_phase"
        ),
        sa.CheckConstraint(
            "weekly_collected >= 0", name="ck_schedule_weekly_collected"
        ),
        sa.CheckConstraint("weekly_target >= 0", name="ck_schedule_weekly_target"),
        sa.CheckConstraint(
            "(active_job_key IS NULL AND active_job_type IS NULL) OR "
            "(active_job_key IS NOT NULL AND active_job_type IS NOT NULL)",
            name="ck_schedule_active_job_pair",
        ),
        sa.CheckConstraint(
            "active_job_type IS NULL OR active_job_type IN "
            "('DAILY_MAINTENANCE','WEEKLY_FARM','FINAL_COLLECTION','CLAIM_PENDING_GIFT')",
            name="ck_schedule_active_job_type",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("account_id"),
        sa.UniqueConstraint("active_job_key", name="uq_account_schedule_active_job"),
    )


def downgrade() -> None:
    op.drop_table("account_schedule_states")
