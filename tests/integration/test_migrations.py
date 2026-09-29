from alembic import command
from sqlalchemy import inspect

from app.db import Database, alembic_config


def test_migration_round_trip(tmp_path):
    url = f"sqlite:///{(tmp_path / 'migration.sqlite3').as_posix()}"
    config = alembic_config(url)
    command.upgrade(config, "head")
    db = Database(url)
    names = inspect(db.engine).get_table_names()
    assert "jobs" in names
    assert "gameplay_tasks" in names
    db.dispose()
    command.downgrade(config, "0009_gameplay_attention_terminal")
    db = Database(url)
    assert "COMPLETED" not in str(
        inspect(db.engine).get_check_constraints("gameplay_tasks")
    )
    db.dispose()
    command.downgrade(config, "0008_gameplay_no_reward")
    db = Database(url)
    inspector = inspect(db.engine)
    active_index = next(
        index
        for index in inspector.get_indexes("gameplay_tasks")
        if index["name"] == "uq_gameplay_task_active_account_kind"
    )
    assert "NEEDS_ATTENTION" in active_index["dialect_options"]["sqlite_where"].text
    db.dispose()
    command.downgrade(config, "-1")
    db = Database(url)
    inspector = inspect(db.engine)
    assert "NO_REWARD_AVAILABLE" not in str(
        inspector.get_check_constraints("gameplay_tasks")
    )
    assert "gameplay_tasks" in inspector.get_table_names()
    db.dispose()
    command.downgrade(config, "-1")
    db = Database(url)
    inspector = inspect(db.engine)
    assert "gameplay_tasks" not in inspector.get_table_names()
    assert "worker_commands" in inspector.get_table_names()
    db.dispose()
    command.upgrade(config, "head")
    db = Database(url)
    assert "gameplay_tasks" in inspect(db.engine).get_table_names()
    db.dispose()
    command.downgrade(config, "0005_runtime_bootstrap_token")
    db = Database(url)
    inspector = inspect(db.engine)
    assert "jobs" in inspector.get_table_names()
    assert "worker_commands" not in inspector.get_table_names()
    assert "worker_plugin" not in {
        column["name"] for column in inspector.get_columns("worker_status")
    }
    assert "verified_at" in {
        column["name"] for column in inspector.get_columns("runtime_instances")
    }
    db.dispose()
    command.upgrade(config, "head")
    db = Database(url)
    inspector = inspect(db.engine)
    assert "worker_commands" in inspector.get_table_names()
    assert "gameplay_tasks" in inspector.get_table_names()
    assert "worker_plugin" in {
        column["name"] for column in inspector.get_columns("worker_status")
    }
    db.dispose()
