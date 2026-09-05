from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db import Database
from app.models import (
    Node,
    RuntimeInstance,
    RuntimeState,
    RuntimeViewSession,
    WorkerStatus,
    utcnow,
)
from app.providers.base import RuntimeDescriptor
from app.providers.view import RuntimeViewProvider, ViewStatus, ViewUnavailable
from app.runtime.display import DisplayEnvironment
from app.services.records import add_audit
from app.services.security import ensure_utc, generate_token, hash_token, token_matches
from app.services.workers import WorkerControlError, WorkerControlService


class ViewSessionNotFound(KeyError):
    pass


class ViewSessionNotReady(RuntimeError):
    pass


class ViewService:
    def __init__(
        self,
        db: Database,
        provider: RuntimeViewProvider,
        *,
        ttl_seconds: int,
        display: DisplayEnvironment,
        workers: WorkerControlService,
    ):
        self.db = db
        self.provider = provider
        self.ttl_seconds = ttl_seconds
        self.display = display
        self.workers = workers

    def _descriptor(self, runtime_id: int) -> tuple[RuntimeDescriptor, int]:
        with self.db.transaction(immediate=True) as session:
            runtime = session.get(RuntimeInstance, runtime_id)
            if (
                runtime is None
                or not runtime.active
                or runtime.state == RuntimeState.DESTROYED
            ):
                raise ViewSessionNotFound(runtime_id)
            node = session.get(Node, runtime.node_id)
            if node is None:
                raise ViewSessionNotFound(runtime.node_id)
            return RuntimeDescriptor(
                id=runtime.id,
                account_id=runtime.account_id,
                node_id=runtime.node_id,
                external_id=runtime.external_id,
                provider=runtime.provider,
                incus_remote=node.incus_remote,
                image_version=runtime.image_version,
                runtime_generation=runtime.runtime_generation,
                network_profile=runtime.network_profile,
            ), runtime.account_id

    def create(
        self,
        runtime_id: int,
        *,
        admin_user_id: int,
        actor: str,
        request_id: str,
        mode: str = "VIEW_ONLY",
    ) -> dict:
        if mode not in {"VIEW_ONLY", "INTERACTIVE"}:
            raise ValueError("invalid view mode")
        descriptor, account_id = self._descriptor(runtime_id)
        access_token = generate_token()
        expires_at = utcnow() + timedelta(seconds=self.ttl_seconds)
        superseded: list[str] = []
        with self.db.transaction(immediate=True) as session:
            previous = list(
                session.scalars(
                    select(RuntimeViewSession).where(
                        RuntimeViewSession.runtime_id == runtime_id,
                        RuntimeViewSession.closed_at.is_(None),
                        RuntimeViewSession.status.in_(
                            ["CREATING", "ACTIVE", "PENDING"]
                        ),
                    )
                )
            )
            for old in previous:
                old.status = "CLOSED"
                old.closed_at = utcnow()
                if old.backend_session_id:
                    superseded.append(old.backend_session_id)
            record = RuntimeViewSession(
                runtime_id=runtime_id,
                admin_user_id=admin_user_id,
                created_by=actor,
                token_hash=hash_token(access_token),
                backend=self.provider.name,
                mode=mode,
                status="CREATING",
                expires_at=expires_at,
            )
            session.add(record)
            session.flush()
            view_session_id = record.id
            add_audit(
                session,
                actor=actor,
                action="CREATE_VIEW_SESSION",
                entity_type="runtime",
                entity_id=runtime_id,
                request_id=request_id,
                metadata={
                    "view_session_id": record.id,
                    "mode": mode,
                    "backend": self.provider.name,
                },
            )
        for backend_id in superseded:
            try:
                self.provider.close_session(backend_id)
            except ViewUnavailable:
                pass
        if mode == "INTERACTIVE":
            try:
                self.workers.command(
                    account_id,
                    "PAUSE",
                    actor=actor,
                    request_id=request_id,
                    payload={
                        "reason": "INTERACTIVE_VIEW",
                        "view_session_id": view_session_id,
                    },
                )
            except WorkerControlError as exc:
                with self.db.transaction(immediate=True) as session:
                    record = session.get(RuntimeViewSession, view_session_id)
                    if record:
                        record.status = "ERROR"
                        record.error_code = "REMOTE_VIEW_UNAVAILABLE"
                        record.error_message = (
                            "worker pause command could not be queued"
                        )
                raise ViewUnavailable(
                    "worker pause command could not be queued"
                ) from exc
        try:
            self.provider.prepare_runtime(descriptor, self.display)
            provider_status = self.provider.create_session(descriptor, self.display)
        except Exception as exc:
            code = getattr(exc, "code", "REMOTE_VIEW_BACKEND_UNAVAILABLE")
            with self.db.transaction(immediate=True) as session:
                record = session.get(RuntimeViewSession, view_session_id)
                if record:
                    record.status = "ERROR"
                    record.error_code = str(code)
                    record.error_message = str(exc)[:1000]
            if isinstance(exc, ViewUnavailable):
                raise
            raise ViewUnavailable(str(exc)) from exc
        with self.db.transaction(immediate=True) as session:
            record = session.get(RuntimeViewSession, view_session_id)
            assert record is not None
            record.backend_session_id = provider_status.backend_session_id
            record.provider_session_id = provider_status.backend_session_id
            record.status = (
                "CREATING" if mode == "INTERACTIVE" else provider_status.status
            )
        return {
            "id": view_session_id,
            "runtime_id": runtime_id,
            "status": "CREATING" if mode == "INTERACTIVE" else provider_status.status,
            "mode": mode,
            "backend": self.provider.name,
            "expires_at": expires_at.isoformat(),
            "access_token": access_token,
            "transport": f"/api/v1/view-sessions/{view_session_id}/transport/",
        }

    def _authorized_record(self, view_session_id: int, access_token: str | None):
        with self.db.transaction(immediate=True) as session:
            record = session.get(RuntimeViewSession, view_session_id)
            if (
                record is None
                or record.closed_at is not None
                or not token_matches(access_token or "", record.token_hash)
            ):
                raise ViewSessionNotFound(view_session_id)
            if ensure_utc(record.expires_at) <= utcnow():
                record.status = "EXPIRED"
                record.closed_at = utcnow()
                backend_id = record.backend_session_id
            else:
                record.last_access_at = utcnow()
                return {
                    "id": record.id,
                    "runtime_id": record.runtime_id,
                    "mode": record.mode,
                    "status": record.status,
                    "backend_id": record.backend_session_id,
                    "expires_at": record.expires_at,
                }
        if backend_id:
            self.provider.close_session(backend_id)
        raise ViewSessionNotFound(view_session_id)

    def status(self, view_session_id: int, access_token: str | None) -> dict:
        item = self._authorized_record(view_session_id, access_token)
        if not item["backend_id"]:
            return {
                "id": view_session_id,
                "runtime_id": item["runtime_id"],
                "status": "CREATING",
            }
        try:
            backend = self.provider.status(item["backend_id"])
        except ViewUnavailable as exc:
            backend = ViewStatus(
                "ERROR", item["backend_id"], str(exc.code), str(exc)[:1000]
            )
        status = backend.status
        if item["mode"] == "INTERACTIVE" and status == "ACTIVE":
            with self.db.session() as session:
                worker = session.get(WorkerStatus, item["runtime_id"])
                worker_state = worker.automation_state if worker else "UNKNOWN"
            if worker_state not in {"PAUSED", "DISABLED", "STOPPED", "NOOP"}:
                status = "CREATING"
        with self.db.transaction(immediate=True) as session:
            record = session.get(RuntimeViewSession, view_session_id)
            if record:
                record.status = status
                record.error_code = backend.error_code
                record.error_message = backend.error_message
        return {
            "id": view_session_id,
            "runtime_id": item["runtime_id"],
            "status": status,
            "mode": item["mode"],
            "expires_at": item["expires_at"].isoformat(),
            "transport": f"/api/v1/view-sessions/{view_session_id}/transport/"
            if status == "ACTIVE"
            else None,
        }

    def resolve_upstream(
        self, view_session_id: int, access_token: str | None
    ) -> tuple[str, int, float]:
        item = self._authorized_record(view_session_id, access_token)
        current = self.status(view_session_id, access_token)
        if current["status"] != "ACTIVE" or not item["backend_id"]:
            raise ViewSessionNotReady("view session is not active")
        host, port = self.provider.resolve_upstream(item["backend_id"])
        remaining = max(
            0.0, (ensure_utc(item["expires_at"]) - utcnow()).total_seconds()
        )
        if remaining <= 0:
            raise ViewSessionNotFound(view_session_id)
        return host, port, remaining

    def close(self, view_session_id: int, *, actor: str, request_id: str) -> None:
        with self.db.transaction(immediate=True) as session:
            record = session.get(RuntimeViewSession, view_session_id)
            if record is None:
                raise ViewSessionNotFound(view_session_id)
            backend_id = record.backend_session_id
            record.closed_at = utcnow()
            record.status = "CLOSED"
            add_audit(
                session,
                actor=actor,
                action="CLOSE_VIEW_SESSION",
                entity_type="runtime_view_session",
                entity_id=view_session_id,
                request_id=request_id,
            )
        if backend_id:
            self.provider.close_session(backend_id)

    def cleanup_expired_sessions(self) -> int:
        now = utcnow()
        with self.db.transaction(immediate=True) as session:
            records = list(
                session.scalars(
                    select(RuntimeViewSession)
                    .where(
                        RuntimeViewSession.closed_at.is_(None),
                        RuntimeViewSession.expires_at <= now,
                    )
                    .limit(100)
                )
            )
            backend_ids = [
                item.backend_session_id for item in records if item.backend_session_id
            ]
            for item in records:
                item.status = "EXPIRED"
                item.closed_at = now
        self.provider.cleanup_expired_sessions(backend_ids)
        return len(records)
