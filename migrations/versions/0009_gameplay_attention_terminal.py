"""Allow a fresh attempt while preserving an attention task's history."""

import sqlalchemy as sa
from alembic import op

revision = "0009_gameplay_attention_terminal"
down_revision = "0008_gameplay_no_reward"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_gameplay_task_active_account_kind", table_name="gameplay_tasks")
    op.create_index(
        "uq_gameplay_task_active_account_kind",
        "gameplay_tasks",
        ["account_id", "kind"],
        unique=True,
        sqlite_where=sa.text("status IN ('PENDING','RUNNING')"),
        postgresql_where=sa.text("status IN ('PENDING','RUNNING')"),
    )


def downgrade() -> None:
    op.drop_index("uq_gameplay_task_active_account_kind", table_name="gameplay_tasks")
    op.create_index(
        "uq_gameplay_task_active_account_kind",
        "gameplay_tasks",
        ["account_id", "kind"],
        unique=True,
        sqlite_where=sa.text("status IN ('PENDING','RUNNING','NEEDS_ATTENTION')"),
        postgresql_where=sa.text("status IN ('PENDING','RUNNING','NEEDS_ATTENTION')"),
    )
