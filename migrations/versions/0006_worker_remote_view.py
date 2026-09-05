"""Add concrete remote-view metadata and isolated worker control state."""

import sqlalchemy as sa
from alembic import op

revision = "0006_worker_remote_view"
down_revision = "0005_runtime_bootstrap_token"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runtime_view_sessions") as batch:
        batch.add_column(sa.Column("admin_user_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "backend",
                sa.String(length=32),
                nullable=False,
                server_default="disabled",
            )
        )
        batch.add_column(
            sa.Column("backend_session_id", sa.String(length=255), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "mode", sa.String(length=16), nullable=False, server_default="VIEW_ONLY"
            )
        )
        batch.add_column(
            sa.Column("last_access_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("error_code", sa.String(length=80), nullable=True))
        batch.add_column(sa.Column("error_message", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_view_admin_user",
            "admin_users",
            ["admin_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_runtime_view_sessions_admin_user_id", ["admin_user_id"])
    op.execute(
        "UPDATE runtime_view_sessions SET backend_session_id = provider_session_id WHERE backend_session_id IS NULL"
    )

    with op.batch_alter_table("worker_status") as batch:
        batch.add_column(
            sa.Column(
                "worker_plugin",
                sa.String(length=80),
                nullable=False,
                server_default="noop",
            )
        )
        batch.add_column(
            sa.Column(
                "worker_version",
                sa.String(length=32),
                nullable=False,
                server_default="1.0.0",
            )
        )
        batch.add_column(
            sa.Column(
                "worker_config_version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )
        batch.add_column(
            sa.Column(
                "worker_mode",
                sa.String(length=16),
                nullable=False,
                server_default="DISABLED",
            )
        )
        batch.add_column(
            sa.Column("last_tick_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("last_action", sa.String(length=80), nullable=True))
        batch.add_column(
            sa.Column("last_observation_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("error_code", sa.String(length=80), nullable=True))
        batch.add_column(
            sa.Column("restart_count", sa.Integer(), nullable=False, server_default="0")
        )

    op.create_table(
        "worker_commands",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "runtime_id",
            sa.Integer(),
            sa.ForeignKey("runtime_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("command", sa.String(length=24), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.String(length=80), nullable=True),
    )
    op.create_index("ix_worker_commands_runtime_id", "worker_commands", ["runtime_id"])
    op.create_index("ix_worker_commands_status", "worker_commands", ["status"])
    op.create_table(
        "worker_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "runtime_id",
            sa.Integer(),
            sa.ForeignKey("runtime_instances.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("plugin", sa.String(length=80), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "worker_active_seconds", sa.Float(), nullable=False, server_default="0"
        ),
        sa.Column("pause_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("actions_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recoveries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", sa.String(length=40), nullable=True),
    )
    op.create_index("ix_worker_runs_runtime_id", "worker_runs", ["runtime_id"])
    op.create_index("ix_worker_runs_account_id", "worker_runs", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_worker_runs_account_id", table_name="worker_runs")
    op.drop_index("ix_worker_runs_runtime_id", table_name="worker_runs")
    op.drop_table("worker_runs")
    op.drop_index("ix_worker_commands_status", table_name="worker_commands")
    op.drop_index("ix_worker_commands_runtime_id", table_name="worker_commands")
    op.drop_table("worker_commands")
    with op.batch_alter_table("worker_status") as batch:
        for column in (
            "restart_count",
            "error_code",
            "last_observation_at",
            "last_action",
            "last_tick_at",
            "worker_mode",
            "worker_config_version",
            "worker_version",
            "worker_plugin",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("runtime_view_sessions") as batch:
        batch.drop_index("ix_runtime_view_sessions_admin_user_id")
        batch.drop_constraint("fk_view_admin_user", type_="foreignkey")
        for column in (
            "error_message",
            "error_code",
            "last_access_at",
            "mode",
            "backend_session_id",
            "backend",
            "admin_user_id",
        ):
            batch.drop_column(column)
