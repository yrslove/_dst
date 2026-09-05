from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent, Event


def add_event(
    session: Session,
    *,
    level: str,
    kind: str,
    message: str,
    account_id: int | None = None,
    runtime_id: int | None = None,
    node_id: int | None = None,
    request_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Event:
    event = Event(
        account_id=account_id,
        runtime_id=runtime_id,
        node_id=node_id,
        level=level,
        kind=kind,
        message=message[:2000],
        metadata_json=metadata or {},
        request_id=request_id,
    )
    session.add(event)
    return event


def add_audit(
    session: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: int | str,
    request_id: str | None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    record = AuditEvent(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        request_id=request_id,
        metadata_json=metadata or {},
    )
    session.add(record)
    return record
