from __future__ import annotations

import logging
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
from app.services.execution_lock import execution_lock
from app.services.records import add_audit
from app.services.security import ensure_utc, generate_token, hash_token, token_matches
from app.services.workers import WorkerControlError, WorkerControlService

logger = logging.getLogger("runtime.views")


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
            if runtime.state != RuntimeState.RUNNING:
                raise ViewUnavailable("runtime must be running before opening a view")
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
        with execution_lock(self.db, account_id) as acquired:
            if not acquired:
                raise ViewUnavailable(
                    "runtime lifecycle or view operation is already in progress"
                )
            # The runtime could have changed while this caller waited for the
            # account-scoped lock. Revalidate before any provider side effect.
            descriptor, account_id = self._descriptor(runtime_id)
            return self._create_locked(
                runtime_id,
                descriptor=descriptor,
                account_id=account_id,
                admin_user_id=admin_user_id,
                actor=actor,
                request_id=request_id,
                mode=mode,
            )

    def _create_locked(
        self,
        runtime_id: int,
        *,
        descriptor: RuntimeDescriptor,
        account_id: int,
        admin_user_id: int,
        actor: str,
        request_id: str,
        mode: str,
    ) -> dict:
        access_token = generate_token()
        expires_at = utcnow() + timedelta(seconds=self.ttl_seconds)
        reserved_backend_id = self.provider.reserve_session(descriptor, self.display)
        superseded: list[tuple[int, str | None]] = []
        with self.db.transaction(immediate=True) as session:
            previous = list(
                session.scalars(
                    select(RuntimeViewSession).where(
                        RuntimeViewSession.runtime_id == runtime_id,
                        RuntimeViewSession.closed_at.is_(None),
                        RuntimeViewSession.status.in_(
                            ["CREATING", "ACTIVE", "PENDING", "CLOSING"]
                        ),
                    )
                )
            )
            for old in previous:
                old.status = "CLOSING"
                superseded.append((old.id, old.backend_session_id))
            record = RuntimeViewSession(
                runtime_id=runtime_id,
                admin_user_id=admin_user_id,
                created_by=actor,
                token_hash=hash_token(access_token),
                backend=self.provider.name,
                backend_session_id=reserved_backend_id,
                provider_session_id=reserved_backend_id,
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
        supersede_failure = False
        for old_id, backend_id in superseded:
            try:
                if backend_id:
                    self.provider.close_session(backend_id)
            except ViewUnavailable:
                supersede_failure = True
                logger.warning("superseded view backend cleanup will be retried")
                continue
            with self.db.transaction(immediate=True) as session:
                old = session.get(RuntimeViewSession, old_id)
                if old and old.status == "CLOSING":
                    old.status = "CLOSED"
                    old.closed_at = utcnow()
        if supersede_failure:
            with self.db.transaction(immediate=True) as session:
                record = session.get(RuntimeViewSession, view_session_id)
                if record:
                    record.status = "ERROR"
                    record.error_code = "REMOTE_VIEW_BACKEND_UNAVAILABLE"
                    record.error_message = "previous view backend cleanup failed"
            raise ViewUnavailable("previous view backend cleanup failed")
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
            provider_status = self.provider.create_session(
                descriptor,
                self.display,
                backend_session_id=reserved_backend_id,
            )
        except Exception as exc:
            code = getattr(exc, "code", "REMOTE_VIEW_BACKEND_UNAVAILABLE")
            pending_backend_id = (
                getattr(exc, "backend_session_id", None) or reserved_backend_id
            )
            with self.db.transaction(immediate=True) as session:
                record = session.get(RuntimeViewSession, view_session_id)
                if record:
                    record.backend_session_id = pending_backend_id
                    record.provider_session_id = pending_backend_id
                    record.status = "CLOSING" if pending_backend_id else "ERROR"
                    record.error_code = str(code)
                    record.error_message = str(exc)[:1000]
            if isinstance(exc, ViewUnavailable):
                raise
            raise ViewUnavailable(str(exc)) from exc
        try:
            with self.db.transaction(immediate=True) as session:
                record = session.get(RuntimeViewSession, view_session_id)
                if (
                    record is None
                    or record.closed_at is not None
                    or record.status != "CREATING"
                ):
                    raise ViewUnavailable("view session was superseded during setup")
                record.backend_session_id = provider_status.backend_session_id
                record.provider_session_id = provider_status.backend_session_id
                record.status = (
                    "CREATING" if mode == "INTERACTIVE" else provider_status.status
                )
        except Exception:
            cleanup_failed = False
            if provider_status.backend_session_id:
                try:
                    self.provider.close_session(provider_status.backend_session_id)
                except Exception:
                    cleanup_failed = True
                    logger.exception("failed to clean up unpersisted view backend")
            if cleanup_failed:
                try:
                    with self.db.transaction(immediate=True) as session:
                        record = session.get(RuntimeViewSession, view_session_id)
                        if record and record.closed_at is None:
                            record.backend_session_id = (
                                provider_status.backend_session_id
                            )
                            record.provider_session_id = (
                                provider_status.backend_session_id
                            )
                            record.status = "CLOSING"
                except Exception:
                    logger.exception(
                        "failed to persist pending view backend cleanup"
                    )
            raise
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
        cleanup_required = False
        with self.db.transaction(immediate=True) as session:
            record = session.get(RuntimeViewSession, view_session_id)
            if (
                record is None
                or record.closed_at is not None
                or record.status == "CLOSING"
                or not token_matches(access_token or "", record.token_hash)
            ):
                raise ViewSessionNotFound(view_session_id)
            runtime = session.get(RuntimeInstance, record.runtime_id)
            if (
                runtime is None
                or not runtime.active
                or runtime.state != RuntimeState.RUNNING
                or ensure_utc(record.expires_at) <= utcnow()
            ):
                # Do not stop the shared xpra shadow outside the account lock.
                # CLOSING also prevents a new view from bypassing pending cleanup.
                record.status = "CLOSING"
                cleanup_required = True
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
        if cleanup_required:
            self.cleanup_expired_sessions()
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
        still_open = False
        with self.db.transaction(immediate=True) as session:
            record = session.get(RuntimeViewSession, view_session_id)
            if (
                record
                and record.closed_at is None
                and record.status not in {"CLOSING", "CLOSED", "EXPIRED"}
            ):
                record.status = status
                record.error_code = backend.error_code
                record.error_message = backend.error_message
                still_open = True
        if not still_open:
            raise ViewSessionNotFound(view_session_id)
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
        with self.db.session() as session:
            record = session.get(RuntimeViewSession, view_session_id)
            if record is None:
                raise ViewSessionNotFound(view_session_id)
            if record.closed_at is not None or record.status == "CLOSED":
                return
            runtime = session.get(RuntimeInstance, record.runtime_id)
            if runtime is None:
                raise ViewSessionNotFound(view_session_id)
            account_id = runtime.account_id
        with execution_lock(self.db, account_id) as acquired:
            if not acquired:
                raise ViewUnavailable(
                    "runtime lifecycle or view operation is already in progress"
                )
            self._close_locked(
                view_session_id, actor=actor, request_id=request_id
            )

    def _close_locked(
        self, view_session_id: int, *, actor: str, request_id: str
    ) -> None:
        with self.db.transaction(immediate=True) as session:
            record = session.get(RuntimeViewSession, view_session_id)
            if record is None:
                raise ViewSessionNotFound(view_session_id)
            if record.closed_at is not None or record.status == "CLOSED":
                return
            backend_id = record.backend_session_id
            record.status = "CLOSING"
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
        with self.db.transaction(immediate=True) as session:
            record = session.get(RuntimeViewSession, view_session_id)
            if record:
                record.closed_at = utcnow()
                record.status = "CLOSED"

    def cleanup_expired_sessions(self) -> int:
        now = utcnow()
        with self.db.session() as session:
            records = list(
                session.execute(
                    select(RuntimeViewSession, RuntimeInstance.account_id)
                    .join(
                        RuntimeInstance,
                        RuntimeInstance.id == RuntimeViewSession.runtime_id,
                    )
                    .where(
                        RuntimeViewSession.closed_at.is_(None),
                        (
                            (RuntimeViewSession.expires_at <= now)
                            | (RuntimeViewSession.status == "CLOSING")
                        ),
                    )
                    .limit(100)
                )
            )
            pending = [
                (item.id, account_id, item.status == "CLOSING")
                for item, account_id in records
            ]
        cleaned = 0
        for record_id, account_id, was_closing in pending:
            with execution_lock(self.db, account_id) as acquired:
                if not acquired:
                    continue
                with self.db.transaction(immediate=True) as session:
                    record = session.get(RuntimeViewSession, record_id)
                    if (
                        record is None
                        or record.closed_at is not None
                        or not (
                            ensure_utc(record.expires_at) <= now
                            or record.status == "CLOSING"
                        )
                    ):
                        continue
                    backend_id = record.backend_session_id
                    record.status = "CLOSING"
                try:
                    if backend_id:
                        self.provider.close_session(backend_id)
                except ViewUnavailable:
                    logger.warning("view backend cleanup will be retried")
                    continue
                with self.db.transaction(immediate=True) as session:
                    record = session.get(RuntimeViewSession, record_id)
                    if record and record.closed_at is None:
                        record.status = "CLOSED" if was_closing else "EXPIRED"
                        record.closed_at = now
                        cleaned += 1
        return cleaned
