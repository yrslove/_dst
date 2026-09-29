"""Allow completed session results without weakening gift success constraints."""

from alembic import op

revision = "0010_long_session"
down_revision = "0009_gameplay_attention_terminal"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("gameplay_tasks") as batch:
        batch.drop_constraint("ck_gameplay_task_status", type_="check")
        batch.create_check_constraint(
            "ck_gameplay_task_status",
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED','NEEDS_ATTENTION','NO_REWARD_AVAILABLE','COMPLETED')",
        )
        batch.create_check_constraint(
            "ck_gameplay_session_completion",
            "status != 'COMPLETED' OR (kind = 'LONG_SESSION' AND result_json IS NOT NULL AND completed_at IS NOT NULL)",
        )


def downgrade():
    with op.batch_alter_table("gameplay_tasks") as batch:
        batch.drop_constraint("ck_gameplay_session_completion", type_="check")
        batch.drop_constraint("ck_gameplay_task_status", type_="check")
        batch.create_check_constraint(
            "ck_gameplay_task_status",
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED','NEEDS_ATTENTION','NO_REWARD_AVAILABLE')",
        )
