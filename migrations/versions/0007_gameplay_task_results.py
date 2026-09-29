"""Persist gameplay task progress and idempotent confirmed gift results."""

import sqlalchemy as sa
from alembic import op

revision = "0007_gameplay_task_results"
down_revision = "0006_worker_remote_view"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gameplay_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("runtime_id", sa.Integer(), nullable=False),
        sa.Column("worker_run_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_key", sa.String(length=64), nullable=True),
        sa.Column("claim_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_persisted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED',"
            "'NEEDS_ATTENTION')",
            name="ck_gameplay_task_status",
        ),
        sa.CheckConstraint(
            "status != 'SUCCEEDED' OR "
            "(confirmation_key IS NOT NULL AND claim_confirmed_at IS NOT NULL "
            "AND claim_persisted_at IS NOT NULL AND result_json IS NOT NULL)",
            name="ck_gameplay_task_success_has_claim",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["runtime_id"], ["runtime_instances.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["worker_run_id"], ["worker_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "kind",
            "confirmation_key",
            name="uq_gameplay_task_claim_identity",
        ),
    )
    op.create_index("ix_gameplay_tasks_account_id", "gameplay_tasks", ["account_id"])
    op.create_index("ix_gameplay_tasks_runtime_id", "gameplay_tasks", ["runtime_id"])
    op.create_index(
        "ix_gameplay_tasks_worker_run_id", "gameplay_tasks", ["worker_run_id"]
    )
    op.create_index(
        "uq_gameplay_task_active_account_kind",
        "gameplay_tasks",
        ["account_id", "kind"],
        unique=True,
        sqlite_where=sa.text("status IN ('PENDING','RUNNING','NEEDS_ATTENTION')"),
        postgresql_where=sa.text("status IN ('PENDING','RUNNING','NEEDS_ATTENTION')"),
    )


def downgrade() -> None:
    op.drop_index("uq_gameplay_task_active_account_kind", table_name="gameplay_tasks")
    op.drop_index("ix_gameplay_tasks_worker_run_id", table_name="gameplay_tasks")
    op.drop_index("ix_gameplay_tasks_runtime_id", table_name="gameplay_tasks")
    op.drop_index("ix_gameplay_tasks_account_id", table_name="gameplay_tasks")
    op.drop_table("gameplay_tasks")
