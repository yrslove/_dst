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
        sa.Column("weekly_state", sa.String(length=32), nullable=False),
        sa.Column("weekly_collected", sa.Integer(), nullable=True),
        sa.Column(
            "confirmed_claims_current_observation", sa.Integer(), nullable=False
        ),
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
            "daily_status IN ('UNKNOWN','PENDING','DONE')",
            name="ck_schedule_daily_status",
        ),
        sa.CheckConstraint(
            "weekly_state IN ('UNSYNCED_CURRENT_CYCLE','SYNCED')",
            name="ck_schedule_weekly_state",
        ),
        sa.CheckConstraint(
            "phase IN ('FARMING','FINAL_COLLECTION','DONE')", name="ck_schedule_phase"
        ),
        sa.CheckConstraint(
            "weekly_collected IS NULL OR weekly_collected >= 0",
            name="ck_schedule_weekly_collected",
        ),
        sa.CheckConstraint(
            "(weekly_state = 'UNSYNCED_CURRENT_CYCLE' AND weekly_collected IS NULL) OR "
            "(weekly_state = 'SYNCED' AND weekly_collected IS NOT NULL)",
            name="ck_schedule_weekly_sync_count",
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
    op.execute(
        sa.text(
            "INSERT INTO account_schedule_states "
            "(account_id,daily_status,weekly_state,weekly_collected,"
            "confirmed_claims_current_observation,weekly_target,phase,pending_gift,"
            "next_daily_due_at,next_weekly_eligible_at,estimated_due_at,"
            "last_daily_claim_at,last_weekly_claim_at,estimated_time_to_gift_seconds,"
            "schedule_revision,active_job_key,active_job_type,created_at,updated_at) "
            "SELECT id,'UNKNOWN','UNSYNCED_CURRENT_CYCLE',NULL,0,8,'FARMING',FALSE,"
            "NULL,NULL,NULL,NULL,NULL,NULL,0,NULL,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP "
            "FROM accounts"
        )
    )


def downgrade() -> None:
    op.drop_table("account_schedule_states")
