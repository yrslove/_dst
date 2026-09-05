from alembic import command
from sqlalchemy import inspect

from app.db import Database, alembic_config


def test_migration_round_trip(tmp_path):
    url = f"sqlite:///{(tmp_path / 'migration.sqlite3').as_posix()}"
    config = alembic_config(url)
    command.upgrade(config, "head")
    db = Database(url)
    assert "jobs" in inspect(db.engine).get_table_names()
    db.dispose()
    command.downgrade(config, "-1")
    db = Database(url)
    inspector = inspect(db.engine)
    assert "jobs" in inspector.get_table_names()
    assert "verified_at" not in {
        column["name"] for column in inspector.get_columns("runtime_instances")
    }
    db.dispose()
    command.upgrade(config, "head")
    db = Database(url)
    assert "verified_at" in {
        column["name"] for column in inspect(db.engine).get_columns("runtime_instances")
    }
    db.dispose()
