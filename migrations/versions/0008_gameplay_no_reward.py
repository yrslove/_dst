"""Represent a normal no-reward outcome as a terminal gameplay task state."""

from alembic import op

revision = "0008_gameplay_no_reward"
down_revision = "0007_gameplay_task_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("gameplay_tasks") as batch:
        batch.drop_constraint("ck_gameplay_task_status", type_="check")
        batch.create_check_constraint(
            "ck_gameplay_task_status",
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED',"
            "'NEEDS_ATTENTION','NO_REWARD_AVAILABLE')",
        )


def downgrade() -> None:
    with op.batch_alter_table("gameplay_tasks") as batch:
        batch.drop_constraint("ck_gameplay_task_status", type_="check")
        batch.create_check_constraint(
            "ck_gameplay_task_status",
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED',"
            "'NEEDS_ATTENTION')",
        )
