from __future__ import annotations

import logging
import threading
from datetime import timedelta

from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.domain.errors import ControlPlaneError
from app.domain.state import (
    GAME_READY_PHASES,
    InvalidStateTransition,
    transition_account,
    transition_runtime,
)
from app.logging_config import job_id_var
from app.models import (
    Account,
    AccountState,
    DesiredState,
    ErrorCode,
    Job,
    JobKind,
    Run,
    RuntimeImage,
    RuntimeInstance,
    RuntimeState,
    WorkerStatus,
    utcnow,
)
from app.providers.base import (
    InstanceNotFound,
    ProviderError,
    RuntimeDescriptor,
    RuntimeProvider,
)
from app.runtime.bootstrap import RuntimeBootstrapService
from app.runtime.bootstrap_models import RuntimeAgentConfig
from app.services.execution_lock import execution_lock
from app.services.jobs import JobQueue
from app.services.leases import LeaseService
from app.services.records import add_event
from app.services.runtime_images import RuntimeImageService
from app.services.secrets import SecretsService
from app.services.security import ensure_utc, generate_token, hash_token

logger = logging.getLogger("job.executor")


class VerificationRequired(ControlPlaneError):
    code = ErrorCode.NEEDS_LOGIN


class MoveRequiresManualProcedure(ControlPlaneError):
    code = ErrorCode.NEEDS_LOGIN


class JobExecutor:
    def __init__(
        self,
        db: Database,
        jobs: JobQueue,
        leases: LeaseService,
        provider: RuntimeProvider,
        settings: Settings,
        worker_id: str,
    ):
        self.db = db
        self.jobs = jobs
        self.leases = leases
        self.provider = provider
        self.settings = settings
        self.worker_id = worker_id

    def execute_next(self) -> bool:
        job = self.jobs.claim(self.worker_id)
        if job is None:
            return False
        self.execute(job)
        return True

    def execute(self, job: Job) -> None:
        with execution_lock(self.db, job.account_id or -job.id) as acquired:
            if acquired:
                self._execute_owned(job)

    def _execute_owned(self, job: Job) -> None:
        if not self.jobs.mark_running(job.id, job.lease_owner):
            return
        stopped = threading.Event()

        def renew_lease():
            while not stopped.wait(max(0.05, self.jobs.lease_seconds / 3)):
                try:
                    if not self.jobs.renew(job.id, job.lease_owner):
                        return
                except Exception:  # noqa: BLE001
                    logger.error(
                        "job lease renewal failed",
                        extra={"event": "LEASE_RENEWAL_FAILED"},
                    )
                    return

        renewer = threading.Thread(target=renew_lease, daemon=True)
        renewer.start()
        token = job_id_var.set(job.id)
        try:
            handler = {
                JobKind.SETUP_RUNTIME: self._start,
                JobKind.PROVISION_RUNTIME: self._provision,
                JobKind.START_RUNTIME: self._start,
                JobKind.STOP_RUNTIME: self._stop,
                JobKind.RESTART_RUNTIME: self._restart,
                JobKind.REBUILD_RUNTIME: self._rebuild,
                JobKind.VERIFY_RUNTIME: self._verify,
                JobKind.MOVE_RUNTIME: self._move,
                JobKind.BOOTSTRAP_RUNTIME: self._bootstrap_job,
            }.get(job.kind)
            if handler is None:
                raise ControlPlaneError(f"unknown job kind: {job.kind}")
            handler(job)
        # This is the durable job boundary: unexpected handler bugs must fail the
        # attempt in the database instead of killing the executor loop.
        except Exception as exc:  # noqa: BLE001
            if not self.jobs.owns(job):
                logger.error(
                    "discarded stale executor result", extra={"event": "LEASE_LOST"}
                )
                return
            code, retryable = self._classify(exc)
            self._record_failure(job, code, str(exc), retryable)
            self.jobs.fail(
                job.id,
                job.lease_owner,
                code=code,
                message=str(exc),
                retryable=retryable,
            )
            logger.warning(
                "job failed",
                extra={
                    "event": "JOB_FAILED",
                    "account_id": job.account_id,
                    "runtime_id": job.runtime_id,
                    "node_id": job.node_id,
                },
            )
        else:
            self.jobs.succeed(job.id, job.lease_owner)
            logger.info(
                "job succeeded",
                extra={
                    "event": "JOB_SUCCEEDED",
                    "account_id": job.account_id,
                    "runtime_id": job.runtime_id,
                    "node_id": job.node_id,
                },
            )
        finally:
            stopped.set()
            renewer.join(timeout=2)
            job_id_var.reset(token)

    def _assert_owned(self, session, job: Job) -> Job:
        stored = self.jobs._owned(session, job.id, job.lease_owner)
        if stored is None:
            raise ControlPlaneError("executor lease lost; reconcile provider outcome")
        return stored

    @staticmethod
    def _classify(exc: Exception) -> tuple[str, bool]:
        if isinstance(exc, ProviderError):
            return str(exc.code), bool(exc.retryable)
        if isinstance(exc, ControlPlaneError):
            return str(exc.code), bool(exc.retryable)
        if isinstance(exc, InvalidStateTransition):
            return ErrorCode.UNKNOWN, False
        if isinstance(exc, KeyError):
            return ErrorCode.RUNTIME_NOT_FOUND, False
        return ErrorCode.UNKNOWN, False

    def _descriptor(self, runtime_id: int) -> RuntimeDescriptor:
        with self.db.session() as session:
            runtime = session.get(RuntimeInstance, runtime_id)
            if runtime is None:
                raise KeyError(runtime_id)
            from app.models import Node

            node = session.get(Node, runtime.node_id)
            if node is None:
                raise KeyError(runtime.node_id)
            image = session.get(RuntimeImage, runtime.image_version)
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
                image_source_ref=(
                    image.source_ref
                    if image is not None and image.provider == runtime.provider
                    else None
                ),
            )

    def _provision(self, job: Job) -> None:
        assert job.runtime_id is not None
        with self.db.transaction(immediate=True) as session:
            self._assert_owned(session, job)
            runtime = session.get(RuntimeInstance, job.runtime_id)
            if runtime is None:
                raise KeyError(job.runtime_id)
            if runtime.state in {RuntimeState.NEEDS_LOGIN, RuntimeState.READY}:
                return
            account = session.get(Account, runtime.account_id)
            if runtime.state != RuntimeState.PROVISIONING:
                transition_runtime(runtime, RuntimeState.PROVISIONING)
            if account and account.status != AccountState.PROVISIONING:
                transition_account(account, AccountState.PROVISIONING)
        descriptor = self._descriptor(job.runtime_id)
        RuntimeImageService(self.db).require_verified(
            descriptor.image_version, descriptor.provider
        )
        status = self.provider.ensure(descriptor, correlation_id=job.request_id)
        with self.db.transaction(immediate=True) as session:
            self._assert_owned(session, job)
            runtime = session.get(RuntimeInstance, job.runtime_id)
            assert runtime is not None
            account = session.get(Account, runtime.account_id)
            target = (
                RuntimeState.READY
                if status.state == RuntimeState.READY
                else RuntimeState.NEEDS_LOGIN
            )
            transition_runtime(runtime, target)
            if account:
                transition_account(
                    account,
                    AccountState.READY
                    if target == RuntimeState.READY
                    else AccountState.NEEDS_LOGIN,
                )
            add_event(
                session,
                level="INFO",
                kind="RUNTIME_PROVISIONED",
                message=f"Runtime generation {runtime.runtime_generation} provisioned",
                account_id=runtime.account_id,
                runtime_id=runtime.id,
                node_id=runtime.node_id,
                request_id=job.request_id,
                metadata={"image_version": runtime.image_version},
            )

    def _bootstrap(self, job: Job) -> None:
        assert job.runtime_id is not None
        descriptor = self._descriptor(job.runtime_id)
        with self.db.session() as session:
            runtime = session.get(RuntimeInstance, job.runtime_id)
            if runtime is None or not runtime.runtime_token_enc:
                raise VerificationRequired(
                    "runtime bootstrap token is unavailable; rotate runtime token"
                )
            token = SecretsService(self.settings).decrypt(runtime.runtime_token_enc)
            if not token:
                raise VerificationRequired(
                    "runtime bootstrap token cannot be decrypted"
                )
        config = RuntimeAgentConfig(
            runtime_id=descriptor.id,
            runtime_generation=descriptor.runtime_generation,
            account_id=descriptor.account_id,
            node_id=descriptor.node_id,
            runtime_token=token,
            orchestrator_url=self.settings.orchestrator_public_url,
            protocol_version=self.settings.agent_protocol_version,
            heartbeat_interval=5,
            display=self.settings.runtime_display,
            xauthority=self.settings.runtime_xauthority,
            worker_plugin=self.settings.runtime_worker_plugin,
            steam_enabled=self.settings.runtime_auto_launch_steam,
            dst_enabled=self.settings.runtime_auto_launch_dst,
            steam_command=self.settings.runtime_steam_command,
            dst_command=self.settings.runtime_dst_command,
            display_readiness_timeout=(
                self.settings.runtime_display_readiness_timeout_seconds
            ),
            steam_readiness_timeout=(
                self.settings.runtime_steam_readiness_timeout_seconds
            ),
            dst_readiness_timeout=self.settings.runtime_dst_readiness_timeout_seconds,
        )
        RuntimeBootstrapService(self.db, self.provider).bootstrap(
            descriptor, config, correlation_id=job.request_id
        )

    def _bootstrap_job(self, job: Job) -> None:
        """Safely consume legacy standalone bootstrap jobs.

        Current bootstrap is part of START because Incus exec requires a running
        instance. Older deployments may still have durable BOOTSTRAP_RUNTIME rows.
        A stopped instance defers work to its next SETUP/START; a running one can
        complete the resumable phases immediately.
        """
        assert job.runtime_id is not None
        status = self.provider.inspect(
            self._descriptor(job.runtime_id), correlation_id=job.request_id
        )
        if status.state == RuntimeState.RUNNING:
            self._bootstrap(job)

    def _start(self, job: Job) -> None:
        assert job.runtime_id is not None
        with self.db.transaction(immediate=True) as session:
            self._assert_owned(session, job)
            runtime = session.get(RuntimeInstance, job.runtime_id)
            account = session.get(Account, job.account_id)
            if (
                not runtime
                or not runtime.active
                or not account
                or not account.enabled
                or runtime.desired_state != DesiredState.RUNNING
            ):
                raise VerificationRequired("start command no longer eligible")
            if job.kind != JobKind.SETUP_RUNTIME and runtime.verified_at is None:
                raise VerificationRequired(
                    "runtime must be explicitly verified before START"
                )
        self.leases.claim_slot(job.runtime_id, job.id)
        descriptor = self._descriptor(job.runtime_id)
        status = self.provider.start(descriptor, correlation_id=job.request_id)
        if status.state != RuntimeState.RUNNING:
            raise ProviderError(
                f"provider returned unexpected START state {status.state}"
            )
        # Incus exec is only valid for a running instance. Bootstrap is
        # versioned/idempotent, so normal starts are also able to finish a
        # previously interrupted setup or apply a rotated runtime token.
        self._bootstrap(job)
        now = utcnow()
        with self.db.transaction(immediate=True) as session:
            self._assert_owned(session, job)
            runtime = session.get(RuntimeInstance, job.runtime_id)
            assert runtime is not None
            account = session.get(Account, runtime.account_id)
            transition_runtime(runtime, RuntimeState.RUNNING)
            runtime.last_error_code = None
            runtime.last_error_message = None
            if job.kind == JobKind.SETUP_RUNTIME and account:
                transition_account(account, AccountState.NEEDS_LOGIN)
            elif account and account.status != AccountState.RUNNING:
                transition_account(account, AccountState.RUNNING)
            open_run = session.scalar(
                select(Run).where(Run.runtime_id == runtime.id, Run.ended_at.is_(None))
            )
            if open_run is None:
                session.add(
                    Run(
                        account_id=runtime.account_id,
                        runtime_id=runtime.id,
                        node_id=runtime.node_id,
                        started_at=now,
                        start_reason=str(job.payload.get("reason", "manual")),
                    )
                )
            add_event(
                session,
                level="INFO",
                kind="RUNTIME_STARTED",
                message="Runtime started",
                account_id=runtime.account_id,
                runtime_id=runtime.id,
                node_id=runtime.node_id,
                request_id=job.request_id,
            )

    def _stop(self, job: Job) -> None:
        assert job.runtime_id is not None
        descriptor = self._descriptor(job.runtime_id)
        with self.db.transaction(immediate=True) as session:
            self._assert_owned(session, job)
            runtime = session.get(RuntimeInstance, job.runtime_id)
            assert runtime is not None
            if runtime.state not in {
                RuntimeState.STOPPED,
                RuntimeState.NEW,
                RuntimeState.NEEDS_LOGIN,
            }:
                transition_runtime(runtime, RuntimeState.STOPPING)
        try:
            status = self.provider.stop(descriptor, correlation_id=job.request_id)
            if status.state != RuntimeState.STOPPED:
                raise ProviderError(
                    f"provider returned unexpected STOP state {status.state}"
                )
        finally:
            # A successful state update below is required before capacity is reusable.
            pass
        now = utcnow()
        with self.db.transaction(immediate=True) as session:
            self._assert_owned(session, job)
            runtime = session.get(RuntimeInstance, job.runtime_id)
            assert runtime is not None
            account = session.get(Account, runtime.account_id)
            if runtime.state != RuntimeState.STOPPED:
                transition_runtime(runtime, RuntimeState.STOPPED)
            if account and account.enabled:
                transition_account(
                    account,
                    AccountState.READY
                    if runtime.verified_at
                    else AccountState.NEEDS_LOGIN,
                )
            run = session.scalar(
                select(Run)
                .where(Run.runtime_id == runtime.id, Run.ended_at.is_(None))
                .order_by(Run.id.desc())
            )
            if run:
                run.ended_at = now
                run.duration_seconds = max(
                    0,
                    int(
                        (
                            now - ensure_utc(run.started_at)
                        ).total_seconds()
                    ),
                )
                run.result = "STOPPED"
            add_event(
                session,
                level="INFO",
                kind="RUNTIME_STOPPED",
                message="Runtime stopped",
                account_id=runtime.account_id,
                runtime_id=runtime.id,
                node_id=runtime.node_id,
                request_id=job.request_id,
            )
        self.leases.release_slot(job.runtime_id)

    def _restart(self, job: Job) -> None:
        # Durable STOP/START checkpoint: retrying after START never restarts an
        # already running container a second time for the same command.
        if not job.payload.get("restart_stopped"):
            self._stop(job)
            with self.db.transaction(immediate=True) as session:
                stored = self._assert_owned(session, job)
                stored.payload = {**stored.payload, "restart_stopped": True}
                job.payload = dict(stored.payload)
        self._start(job)

    def _verify(self, job: Job) -> None:
        assert job.runtime_id is not None
        if (
            self.provider.inspect(self._descriptor(job.runtime_id)).state
            != RuntimeState.RUNNING
        ):
            raise VerificationRequired("SETUP must boot the runtime before VERIFY")
        with self.db.transaction(immediate=True) as session:
            self._assert_owned(session, job)
            runtime = session.get(RuntimeInstance, job.runtime_id)
            if runtime is None:
                raise KeyError(job.runtime_id)
            worker = session.get(WorkerStatus, runtime.id)
            if (
                worker is None
                or not worker.healthy
                or not worker.steam_running
                or not worker.dst_running
                or worker.phase not in GAME_READY_PHASES
                or ensure_utc(worker.updated_at)
                < utcnow() - timedelta(seconds=self.settings.watchdog_stale_seconds)
            ):
                raise VerificationRequired("runtime agent has not reported GAME_READY")
            runtime.verified_at = utcnow()
            if runtime.state != RuntimeState.RUNNING:
                transition_runtime(runtime, RuntimeState.RUNNING)
            account = session.get(Account, runtime.account_id)
            if account and account.status != AccountState.RUNNING:
                transition_account(account, AccountState.RUNNING)
            add_event(
                session,
                level="INFO",
                kind="RUNTIME_VERIFIED",
                message="Runtime agent readiness verified",
                account_id=runtime.account_id,
                runtime_id=runtime.id,
                node_id=runtime.node_id,
                request_id=job.request_id,
            )

    def _rebuild(self, job: Job) -> None:
        assert job.runtime_id is not None
        old = self._descriptor(job.runtime_id)
        # Validate before inspect/stop/deactivation. This is intentionally also
        # enforced at command admission, but durable jobs may predate that check.
        RuntimeImageService(self.db).require_verified(
            str(job.payload.get("image_version") or self.settings.current_image_version),
            old.provider,
        )
        if job.payload.get("new_runtime_id"):
            self._finish_rebuild(job, old, int(job.payload["new_runtime_id"]))
            return
        with self.db.session() as session:
            old_model = session.get(RuntimeInstance, job.runtime_id)
            if old_model is None:
                raise KeyError(job.runtime_id)
            was_running = old_model.state in {
                RuntimeState.STARTING,
                RuntimeState.RUNNING,
                RuntimeState.STOPPING,
            }
        try:
            observed = self.provider.inspect(old, correlation_id=job.request_id)
            was_running = observed.state != RuntimeState.STOPPED
        except InstanceNotFound:
            was_running = False
        if was_running:
            with self.db.transaction(immediate=True) as session:
                old_model = session.get(RuntimeInstance, job.runtime_id)
                assert old_model is not None
                if old_model.state != RuntimeState.STOPPING:
                    transition_runtime(old_model, RuntimeState.STOPPING)
            status = self.provider.stop(old, correlation_id=job.request_id)
            if status.state != RuntimeState.STOPPED:
                raise ProviderError("rebuild requires confirmed stopped old generation")
        self.leases.release_slot(old.id)
        with self.db.transaction(immediate=True) as session:
            stored_job = self._assert_owned(session, job)
            old_model = session.get(RuntimeInstance, old.id)
            assert old_model is not None
            account = session.get(Account, old_model.account_id)
            if old_model.state not in {
                RuntimeState.STOPPED,
                RuntimeState.NEEDS_LOGIN,
                RuntimeState.NEW,
                RuntimeState.READY,
            }:
                transition_runtime(old_model, RuntimeState.STOPPED)
            old_model.active = False
            session.flush()
            generation = old_model.runtime_generation + 1
            new_runtime = RuntimeInstance(
                account_id=old_model.account_id,
                node_id=old_model.node_id,
                provider=old_model.provider,
                external_id=f"dst-{old_model.account_id:06d}-g{generation}",
                network_profile=old_model.network_profile,
                runtime_generation=generation,
                image_version=str(
                    job.payload.get("image_version")
                    or self.settings.current_image_version
                ),
                state=RuntimeState.PROVISIONING,
                desired_state=DesiredState.STOPPED,
                active=True,
                token_hash=hash_token(runtime_token := generate_token()),
                runtime_token_enc=SecretsService(self.settings).encrypt(runtime_token),
            )
            session.add(new_runtime)
            if account and account.status != AccountState.PROVISIONING:
                transition_account(account, AccountState.PROVISIONING)
            session.flush()
            new_id = new_runtime.id
            stored_job.payload = {**stored_job.payload, "new_runtime_id": new_id}
            job.payload = dict(stored_job.payload)
            run = session.scalar(
                select(Run).where(Run.runtime_id == old.id, Run.ended_at.is_(None))
            )
            if run:
                run.ended_at = utcnow()
                run.duration_seconds = max(
                    0, int((run.ended_at - ensure_utc(run.started_at)).total_seconds())
                )
                run.result = "REBUILT"
        self._finish_rebuild(job, old, new_id)

    def _finish_rebuild(self, job: Job, old: RuntimeDescriptor, new_id: int) -> None:
        descriptor = self._descriptor(new_id)
        RuntimeImageService(self.db).require_verified(
            descriptor.image_version, descriptor.provider
        )
        self.provider.ensure(descriptor, correlation_id=job.request_id)
        with self.db.transaction(immediate=True) as session:
            self._assert_owned(session, job)
            runtime = session.get(RuntimeInstance, new_id)
            assert runtime is not None
            account = session.get(Account, runtime.account_id)
            transition_runtime(runtime, RuntimeState.NEEDS_LOGIN)
            if account:
                transition_account(account, AccountState.NEEDS_LOGIN)
            add_event(
                session,
                level="INFO",
                kind="RUNTIME_REBUILT",
                message=f"Runtime rebuilt as generation {runtime.runtime_generation}",
                account_id=runtime.account_id,
                runtime_id=runtime.id,
                node_id=runtime.node_id,
                request_id=job.request_id,
                metadata={
                    "previous_runtime_id": old.id,
                    "image_version": runtime.image_version,
                },
            )

    def _move(self, job: Job) -> None:
        assert job.runtime_id is not None
        with self.db.session() as session:
            runtime = session.get(RuntimeInstance, job.runtime_id)
            if runtime is None:
                raise KeyError(job.runtime_id)
            if runtime.state in {
                RuntimeState.STARTING,
                RuntimeState.RUNNING,
                RuntimeState.STOPPING,
            }:
                raise MoveRequiresManualProcedure(
                    "running runtime cannot be reassigned"
                )
        raise MoveRequiresManualProcedure(
            "MOVE_RUNTIME requires the documented export/session-restore/verify procedure"
        )

    def _record_failure(
        self, job: Job, code: str, message: str, retryable: bool
    ) -> None:
        if job.runtime_id is None:
            return
        with self.db.transaction(immediate=True) as session:
            self._assert_owned(session, job)
            runtime = session.get(
                RuntimeInstance, job.payload.get("new_runtime_id", job.runtime_id)
            )
            if runtime is None:
                return
            runtime.last_error_code = code
            runtime.last_error_message = message[:2000]
            account = session.get(Account, runtime.account_id)
            if (
                job.kind
                in {
                    JobKind.START_RUNTIME,
                    JobKind.SETUP_RUNTIME,
                    JobKind.RESTART_RUNTIME,
                }
                and retryable
            ):
                if runtime.state == RuntimeState.STARTING:
                    transition_runtime(runtime, RuntimeState.STALE)
                if account and account.status not in {
                    AccountState.NEEDS_ATTENTION,
                    AccountState.DISABLED,
                }:
                    transition_account(account, AccountState.NEEDS_ATTENTION)
            elif (
                job.kind == JobKind.REBUILD_RUNTIME
                and code == ErrorCode.RUNTIME_IMAGE_NOT_VERIFIED
                and not job.payload.get("new_runtime_id")
            ):
                # Rejected before provider side effects: preserve the healthy old
                # generation instead of poisoning it with an ERROR state.
                pass
            elif job.kind == JobKind.BOOTSTRAP_RUNTIME and runtime.state in {
                RuntimeState.NEEDS_LOGIN,
                RuntimeState.READY,
                RuntimeState.STOPPED,
            }:
                # A legacy bootstrap job performs no mutation while stopped.
                # Inspection outages must not turn a safe stopped runtime ERROR.
                pass
            elif code != ErrorCode.NEEDS_LOGIN:
                if runtime.state not in {RuntimeState.DESTROYED, RuntimeState.ERROR}:
                    try:
                        transition_runtime(runtime, RuntimeState.ERROR)
                    except InvalidStateTransition:
                        pass
                if account and account.status not in {
                    AccountState.ERROR,
                    AccountState.DISABLED,
                }:
                    try:
                        transition_account(account, AccountState.ERROR)
                    except InvalidStateTransition:
                        pass
            add_event(
                session,
                level="ERROR",
                kind=code,
                message=message,
                account_id=runtime.account_id,
                runtime_id=runtime.id,
                node_id=runtime.node_id,
                request_id=job.request_id,
                metadata={"job_id": job.id, "retryable": retryable},
            )
