"""Account-scoped lifecycle exclusion, also respected by lease reclamation.

PostgreSQL uses a dedicated, unpooled session across provider I/O. SQLite's
single-host development equivalent is an OS file lock. Neither is a fencing
token understood by Incus: unknown provider outcomes retain their slot.
"""

from __future__ import annotations

import hashlib
import tempfile
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import text

from app.db import Database


@contextmanager
def execution_lock(db: Database, account_id: int):
    if db.is_sqlite:
        # OS locks are released on process death, unlike lock-file existence.
        import os

        database = str(Path(db.engine.url.database or ":memory:").resolve())
        digest = hashlib.sha256(database.encode()).hexdigest()[:24]
        directory = Path(tempfile.gettempdir()) / "dst-control-plane-locks"
        directory.mkdir(exist_ok=True)
        with (directory / f"{digest}-{account_id}.lock").open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            acquired = False
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                pass
            try:
                yield acquired
            finally:
                if acquired:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return

    # Do not return a session-level advisory lock to a pool. Detach means close
    # below physically closes its connection even if unlock encounters an error.
    with db.engine.connect() as connection:
        connection.detach()
        acquired = bool(
            connection.scalar(
                text("SELECT pg_try_advisory_lock(1146311728, :account_id)"),
                {"account_id": account_id},
            )
        )
        connection.commit()
        try:
            yield acquired
        finally:
            if acquired:
                connection.execute(
                    text("SELECT pg_advisory_unlock(1146311728, :account_id)"),
                    {"account_id": account_id},
                )
                connection.commit()
