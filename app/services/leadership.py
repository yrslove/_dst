from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from app.db import Database
from app.models import SchedulerLeadership, utcnow

logger = logging.getLogger("scheduler.leadership")


class SchedulerLeadershipService:
    """Database lease with a monotonically increasing fencing token."""

    def __init__(
        self,
        db: Database,
        holder_id: str,
        *,
        lease_seconds: int = 15,
        name: str = "global-scheduler",
    ):
        self.db, self.holder_id, self.lease_seconds, self.name = (
            db,
            holder_id,
            lease_seconds,
            name,
        )
        self._token: int | None = None

    def try_acquire(self) -> bool:
        now = utcnow()
        with self.db.transaction(immediate=True) as session:
            statement = select(SchedulerLeadership).where(
                SchedulerLeadership.name == self.name
            )
            if not self.db.is_sqlite:
                statement = statement.with_for_update()
            record = session.scalar(statement)
            if record is None:
                record = SchedulerLeadership(
                    name=self.name,
                    holder_id=self.holder_id,
                    fencing_token=1,
                    lease_until=now + timedelta(seconds=self.lease_seconds),
                    updated_at=now,
                )
                session.add(record)
                self._token = 1
                logger.info(
                    "scheduler leadership acquired",
                    extra={"event": "scheduler.leader_acquired"},
                )
                return True
            expired = record.lease_until is None or record.lease_until <= now
            if record.holder_id == self.holder_id or expired:
                if record.holder_id != self.holder_id:
                    record.fencing_token += 1
                    logger.info(
                        "scheduler leadership acquired",
                        extra={"event": "scheduler.leader_acquired"},
                    )
                record.holder_id = self.holder_id
                record.lease_until = now + timedelta(seconds=self.lease_seconds)
                record.updated_at = now
                self._token = record.fencing_token
                return True
        self._lost()
        return False

    def renew(self) -> bool:
        return self.try_acquire()

    def is_leader(self) -> bool:
        if self._token is None:
            return False
        with self.db.session() as session:
            record = session.get(SchedulerLeadership, self.name)
            return bool(
                record
                and record.holder_id == self.holder_id
                and record.fencing_token == self._token
                and record.lease_until
                and record.lease_until > utcnow()
            )

    def release(self) -> None:
        with self.db.transaction(immediate=True) as session:
            record = session.get(SchedulerLeadership, self.name)
            if record and record.holder_id == self.holder_id:
                record.lease_until = utcnow()
                record.updated_at = utcnow()
        self._lost()

    def _lost(self) -> None:
        if self._token is not None:
            logger.warning(
                "scheduler leadership lost", extra={"event": "scheduler.leader_lost"}
            )
        self._token = None
