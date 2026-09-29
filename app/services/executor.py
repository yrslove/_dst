from __future__ import annotations

import logging
import threading
import time
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
    GameplayTask,
    GameplayTaskStatus,
    Job,
    JobKind,
    Run,
    RuntimeImage,
    RuntimeInstance,
    RuntimeState,
    WorkerCommand,
    WorkerRun,
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
from app.services.workers import WorkerControlService

logger = logging.getLogger("job.executor")


class VerificationRequired(ControlPlaneError):
    code = ErrorCode.NEEDS_LOGIN


class MoveRequiresManualProcedure(ControlPlaneError):
    code = ErrorCode.NEEDS_LOGIN


from app.services.session import LongSessionMixin


class JobExecutor(LongSessionMixin):
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
                JobKind.DAILY_GIFT_CLAIM: self._daily_gift_claim,
                JobKind.LONG_SESSION: self._long_session,
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
            safe_idle_world=self.settings.runtime_safe_idle_world,
            worker_calibration_profile=self.settings.runtime_worker_calibration_profile,
            worker_calibration_verified=self.settings.runtime_worker_calibration_verified,
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
        RuntimeBootstrapService(self.db, self.provider, version=4).bootstrap(
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
                    int((now - ensure_utc(run.started_at)).total_seconds()),
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

    def _daily_gift_claim(self, job: Job) -> None:
        """Run one durable claim task through managed runtime and worker paths."""
        assert job.account_id is not None and job.runtime_id is not None
        task_id = int(job.payload.get("gameplay_task_id", 0))
        if task_id < 1:
            raise ControlPlaneError("daily gift job has no durable GameplayTask")
        task = self._gameplay_task(task_id)
        if task is None or (task.account_id, task.runtime_id) != (
            job.account_id,
            job.runtime_id,
        ):
            raise ControlPlaneError("daily gift task ownership does not match job")
        if task.status in {
            GameplayTaskStatus.SUCCEEDED,
            GameplayTaskStatus.NO_REWARD_AVAILABLE,
            GameplayTaskStatus.FAILED,
            GameplayTaskStatus.CANCELLED,
            GameplayTaskStatus.NEEDS_ATTENTION,
        }:
            self._reconcile_terminal_task(job, task)
            return

        resumed = task.status == GameplayTaskStatus.RUNNING
        already_succeeded = False
        with self.db.transaction(immediate=True) as session:
            self._assert_owned(session, job)
            current = session.get(GameplayTask, task_id)
            if current is None:
                raise ControlPlaneError("daily gift task disappeared")
            if current.status == GameplayTaskStatus.SUCCEEDED:
                already_succeeded = True
        if already_succeeded:
            current = self._gameplay_task(task_id)
            if current is not None:
                self._reconcile_terminal_task(job, current)
            return

        owned_runtime = job.payload.get("owns_runtime")
        execution_started = utcnow()
        worker_control = WorkerControlService(self.db)
        worker_started = False
        task_error: Exception | None = None
        try:
            runtime = self._runtime_for_task(job.runtime_id)
            if runtime is None or runtime.verified_at is None:
                raise ControlPlaneError("daily gift task requires a verified runtime")
            observed = self.provider.inspect(self._descriptor(job.runtime_id))
            if owned_runtime is None:
                owned_runtime = observed.state == RuntimeState.STOPPED
                self._persist_task_job_payload(
                    job, {"owns_runtime": bool(owned_runtime)}
                )
            if observed.state == RuntimeState.STOPPED:
                self._start(job)
            elif observed.state not in {
                RuntimeState.RUNNING,
                RuntimeState.STARTING,
            }:
                raise ControlPlaneError(
                    f"runtime cannot start safely from {observed.state}"
                )

            ready = self._wait_for_game_ready(
                job.runtime_id,
                after=execution_started,
                timeout=self.settings.gameplay_readiness_timeout_seconds,
            )
            if not ready:
                raise TimeoutError("runtime did not report fresh GAME_READY")

            if resumed:
                # A restarted orchestrator may observe an already-running worker,
                # but never starts a second claim cycle after losing execution state.
                snapshot = self._worker_snapshot(job.runtime_id)
                if not snapshot or snapshot["mode"] != "ACTIVE":
                    self._set_task_attention(
                        task_id,
                        "ORCHESTRATION_RESTARTED",
                        "Task was interrupted before a confirmed result; automatic replay withheld",
                    )
                    return
                active_command = self._latest_active_mode_command(job.runtime_id)
                if active_command is None:
                    self._set_task_attention(
                        task_id,
                        "WORKER_COMMAND_UNRECONCILED",
                        "Interrupted task has no acknowledged ACTIVE worker command",
                    )
                    return
                observation = self._wait_for_fresh_observation(
                    job.runtime_id,
                    after=execution_started,
                    timeout=self.settings.gameplay_observation_timeout_seconds,
                    mode="ACTIVE",
                    command_id=active_command,
                )
                if not self._safe_observation(observation):
                    self._set_task_attention(
                        task_id,
                        "WORKER_OBSERVATION_STALE",
                        "Resumed worker did not provide fresh safe perception",
                    )
                    return
                worker_started = True
            else:
                observe_command = self._request_worker_mode(
                    worker_control, job, "OBSERVE"
                )
                worker_started = True
                observation = self._wait_for_fresh_observation(
                    job.runtime_id,
                    after=utcnow(),
                    timeout=self.settings.gameplay_observation_timeout_seconds,
                    mode="OBSERVE",
                    command_id=observe_command,
                )
                attempts = 0
                while observation is not None and not self._safe_observation(
                    observation
                ):
                    if observation.get("screen") in {
                        "DEAD",
                        "WORLD_RESET_PENDING",
                        "RESET_PENDING",
                    }:
                        self._set_task_attention(
                            task_id,
                            "UNSAFE_WORLD_STATE",
                            f"Worker observed {observation.get('screen')}; automatic reset is disabled",
                        )
                        return
                    if attempts >= 2:
                        break
                    attempts += 1
                    observation = self._wait_for_fresh_observation(
                        job.runtime_id,
                        after=utcnow(),
                        timeout=self.settings.gameplay_observation_timeout_seconds,
                        mode="OBSERVE",
                        command_id=observe_command,
                    )
                if not self._safe_observation(observation):
                    self._set_task_attention(
                        task_id,
                        "WORKER_OBSERVATION_UNSAFE",
                        "Fresh perception did not establish a known safe gameplay state",
                    )
                    return
                if not self._mark_task_running(task_id):
                    return
                active_command = self._request_worker_mode(
                    worker_control, job, "ACTIVE"
                )
                active_observation = self._wait_for_fresh_observation(
                    job.runtime_id,
                    after=utcnow(),
                    timeout=self.settings.gameplay_observation_timeout_seconds,
                    mode="ACTIVE",
                    command_id=active_command,
                )
                if active_observation is None:
                    raise TimeoutError(
                        "GameWorker did not produce a fresh active observation"
                    )

            self._attach_worker_run(task_id, job.runtime_id)
            self._monitor_daily_gift(job, task_id)
        except Exception as exc:  # noqa: BLE001 - durable task records operational failures
            task_error = exc
            self._set_task_failed(task_id, type(exc).__name__, str(exc))
        finally:
            cleanup_error = None
            if worker_started:
                try:
                    self._disable_worker(
                        worker_control,
                        job,
                        timeout=self.settings.gameplay_observation_timeout_seconds,
                    )
                except Exception as exc:  # noqa: BLE001 - cleanup must not erase result
                    cleanup_error = (
                        f"worker disable failed: {type(exc).__name__}: {exc}"
                    )
            if owned_runtime:
                try:
                    self._managed_task_stop(job)
                except Exception as exc:  # noqa: BLE001 - retain gameplay outcome
                    suffix = f"managed runtime stop failed: {type(exc).__name__}: {exc}"
                    cleanup_error = (
                        f"{cleanup_error}; {suffix}" if cleanup_error else suffix
                    )
            if cleanup_error:
                self._record_cleanup_error(task_id, cleanup_error)
        if task_error is not None:
            raise task_error

    def _gameplay_task(self, task_id: int) -> GameplayTask | None:
        with self.db.session() as session:
            task = session.get(GameplayTask, task_id)
            if task is not None:
                session.expunge(task)
            return task

    def _runtime_for_task(self, runtime_id: int) -> RuntimeInstance | None:
        with self.db.session() as session:
            runtime = session.get(RuntimeInstance, runtime_id)
            if runtime is not None:
                session.expunge(runtime)
            return runtime

    def _persist_task_job_payload(self, job: Job, values: dict) -> None:
        with self.db.transaction(immediate=True) as session:
            stored = self._assert_owned(session, job)
            stored.payload = {**stored.payload, **values}
            job.payload = dict(stored.payload)

    def _set_task_attention(self, task_id: int, code: str, message: str) -> None:
        self._finish_gameplay_task(
            task_id, GameplayTaskStatus.NEEDS_ATTENTION, code, message
        )

    def _mark_task_running(self, task_id: int) -> bool:
        with self.db.transaction(immediate=True) as session:
            task = session.get(GameplayTask, task_id)
            if task is None or task.status not in {
                GameplayTaskStatus.PENDING,
                GameplayTaskStatus.RUNNING,
            }:
                return False
            task.status = GameplayTaskStatus.RUNNING
            task.updated_at = utcnow()
            return True

    def _set_task_failed(self, task_id: int, code: str, message: str) -> None:
        self._finish_gameplay_task(task_id, GameplayTaskStatus.FAILED, code, message)

    def _finish_gameplay_task(
        self, task_id: int, status: GameplayTaskStatus, code: str, message: str
    ) -> None:
        with self.db.transaction(immediate=True) as session:
            task = session.get(GameplayTask, task_id)
            if task is None or task.status == GameplayTaskStatus.SUCCEEDED:
                return
            task.status = status
            task.error_code = code[:80]
            task.error_message = message[:2000]
            task.completed_at = utcnow()
            task.updated_at = task.completed_at

    def _record_cleanup_error(self, task_id: int, message: str) -> None:
        with self.db.transaction(immediate=True) as session:
            task = session.get(GameplayTask, task_id)
            if task is None:
                return
            task.result_json = {
                **(task.result_json or {}),
                "cleanup_error": message[:1000],
            }
            task.updated_at = utcnow()

    def _worker_snapshot(self, runtime_id: int) -> dict | None:
        with self.db.session() as session:
            worker = session.get(WorkerStatus, runtime_id)
            if worker is None:
                return None
            details = worker.details if isinstance(worker.details, dict) else {}
            diagnostics = details.get("diagnostics", {})
            agent_worker = (
                diagnostics.get("worker", {}) if isinstance(diagnostics, dict) else {}
            )
            report_details = (
                agent_worker.get("details", {})
                if isinstance(agent_worker, dict)
                else {}
            )
            observation = (
                report_details.get("observation", {})
                if isinstance(report_details, dict)
                else {}
            )
            report_diagnostics = (
                report_details.get("diagnostics", {})
                if isinstance(report_details, dict)
                else {}
            )
            input_safety = (
                report_diagnostics.get("input_safety", {})
                if isinstance(report_diagnostics, dict)
                else {}
            )
            telemetry = (
                agent_worker.get("telemetry", {})
                if isinstance(agent_worker, dict)
                else {}
            )
            return {
                "mode": worker.worker_mode,
                "state": worker.automation_state,
                "phase": worker.phase,
                "healthy": worker.healthy,
                "steam_running": worker.steam_running,
                "dst_running": worker.dst_running,
                "updated_at": worker.updated_at,
                "last_observation_at": worker.last_observation_at,
                "observation": observation,
                "telemetry": telemetry,
                "held_inputs": input_safety.get("held_inputs")
                if isinstance(input_safety, dict)
                else None,
            }

    def _wait_for_game_ready(self, runtime_id: int, *, after, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.db.session() as session:
                worker = session.get(WorkerStatus, runtime_id)
                if (
                    worker is not None
                    and worker.healthy
                    and worker.steam_running
                    and worker.dst_running
                    and worker.phase in GAME_READY_PHASES
                    and ensure_utc(worker.updated_at) >= ensure_utc(after)
                ):
                    return True
            time.sleep(
                min(
                    self.settings.gameplay_poll_interval_seconds,
                    max(0, deadline - time.monotonic()),
                )
            )
        return False

    @staticmethod
    def _request_worker_mode(control, job: Job, mode: str) -> int:
        result = control.command(
            job.account_id,
            "SET_MODE",
            actor=f"gameplay-task:{job.payload['gameplay_task_id']}",
            request_id=job.request_id
            or f"gameplay-task-{job.payload['gameplay_task_id']}",
            payload={"mode": mode},
        )
        return int(result["id"])

    def _wait_for_fresh_observation(
        self, runtime_id: int, *, after, timeout: float, mode: str, command_id: int
    ) -> dict | None:
        deadline = time.monotonic() + timeout
        seen: set[str] = set()
        while time.monotonic() < deadline:
            snapshot = self._worker_snapshot(runtime_id)
            with self.db.session() as session:
                command = session.get(WorkerCommand, command_id)
                acknowledged = command is not None and command.status == "COMPLETED"
                command_failed = acknowledged and command.result not in {"OK", "None"}
                command_result = command.result if command is not None else None
            if command_failed:
                raise ControlPlaneError(f"worker mode command failed: {command_result}")
            if snapshot and snapshot["mode"] == mode:
                observed_at = snapshot["last_observation_at"]
                observation = snapshot["observation"]
                frame_id = (
                    observation.get("source_frame_id")
                    if isinstance(observation, dict)
                    else None
                )
                if (
                    observed_at is not None
                    and ensure_utc(observed_at) >= ensure_utc(after)
                    and acknowledged
                    and isinstance(frame_id, str)
                    and frame_id not in seen
                ):
                    seen.add(frame_id)
                    if len(seen) >= 1:
                        return observation
            time.sleep(
                min(
                    self.settings.gameplay_poll_interval_seconds,
                    max(0, deadline - time.monotonic()),
                )
            )
        return None

    @staticmethod
    def _safe_observation(observation: dict | None) -> bool:
        if not isinstance(observation, dict):
            return False
        screen = observation.get("screen")
        try:
            confidence = float(observation.get("screen_confidence", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        production_ready = observation.get("production_ready")
        if production_ready is None:
            production_ready = bool(
                observation.get("validity") == "VALID"
                and observation.get("calibration_verified") is True
                and observation.get("assets_verified") is True
            )
        safe_screen = bool(
            production_ready
            and isinstance(screen, str)
            and screen
            not in {
                "UNKNOWN",
                "LOADING",
                "PAUSED",
                "DEAD",
                "WORLD_RESET_PENDING",
                "RESET_PENDING",
                "CHARACTER_LOADOUT",
            }
            and confidence >= 0.8
        )
        if not safe_screen:
            return False
        if screen in {"CHARACTER_SELECTION", "CHARACTER_SELECTION_HOVERED"}:
            detections = observation.get("detections")
            if not isinstance(detections, list):
                return False
            anchors = set()
            for item in detections:
                if not isinstance(item, dict):
                    continue
                try:
                    anchor_confidence = float(item.get("confidence", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if (
                    item.get("detected") is True
                    and item.get("verified") is True
                    and isinstance(item.get("bounds"), dict)
                    and anchor_confidence >= 0.94
                ):
                    anchors.add(item.get("kind"))
            if not {"character_select_title", "character_select_players"} <= anchors:
                return False
            if not (
                {"character_select_wilson_icon", "character_select_wilson_hover"}
                & anchors
            ):
                return False
        return True

    def _attach_worker_run(self, task_id: int, runtime_id: int) -> None:
        with self.db.transaction(immediate=True) as session:
            task = session.get(GameplayTask, task_id)
            run = session.scalar(
                select(WorkerRun)
                .where(WorkerRun.runtime_id == runtime_id, WorkerRun.ended_at.is_(None))
                .order_by(WorkerRun.id.desc())
            )
            if task is not None and run is not None:
                task.worker_run_id = run.id
                task.updated_at = utcnow()

    def _latest_active_mode_command(self, runtime_id: int) -> int | None:
        with self.db.session() as session:
            command = session.scalar(
                select(WorkerCommand)
                .where(
                    WorkerCommand.runtime_id == runtime_id,
                    WorkerCommand.command == "SET_MODE",
                    WorkerCommand.status == "COMPLETED",
                )
                .order_by(WorkerCommand.id.desc())
                .limit(1)
            )
            return (
                command.id
                if command is not None and command.payload.get("mode") == "ACTIVE"
                else None
            )

    def _monitor_daily_gift(self, job: Job, task_id: int) -> None:
        deadline = time.monotonic() + self.settings.gameplay_execution_timeout_seconds
        last_frame = None
        unavailable_frames = 0
        while time.monotonic() < deadline:
            task = self._gameplay_task(task_id)
            if task is None or task.status in {
                GameplayTaskStatus.SUCCEEDED,
                GameplayTaskStatus.NO_REWARD_AVAILABLE,
                GameplayTaskStatus.FAILED,
                GameplayTaskStatus.CANCELLED,
                GameplayTaskStatus.NEEDS_ATTENTION,
            }:
                return
            snapshot = self._worker_snapshot(job.runtime_id)
            if snapshot:
                if snapshot["state"] in {"NEEDS_ATTENTION", "ERROR"}:
                    self._set_task_attention(
                        task_id,
                        snapshot["state"],
                        "GameWorker stopped safely before a confirmed gift result",
                    )
                    return
                telemetry = snapshot["telemetry"]
                evidence = telemetry.get("gift_availability_evidence")
                if self._verified_no_reward(snapshot, task, evidence):
                    with self.db.transaction(immediate=True) as session:
                        current = session.get(GameplayTask, task_id)
                        if (
                            current is not None
                            and current.status != GameplayTaskStatus.SUCCEEDED
                        ):
                            current.status = GameplayTaskStatus.NO_REWARD_AVAILABLE
                            current.result_json = {
                                "semantic": "NO_REWARD_AVAILABLE",
                                "gift_icon_evidence": evidence,
                            }
                            current.completed_at = current.updated_at = utcnow()
                            current.error_code = None
                            current.error_message = None
                    return
                observation = snapshot["observation"]
                frame_id = (
                    observation.get("source_frame_id")
                    if isinstance(observation, dict)
                    else None
                )
                screen = (
                    observation.get("screen") if isinstance(observation, dict) else None
                )
                if frame_id and frame_id != last_frame:
                    last_frame = frame_id
                    if screen == "IN_WORLD_IDLE" and telemetry.get(
                        "daily_gift_state"
                    ) in {
                        "UNKNOWN",
                        "GIFT_AVAILABILITY_UNKNOWN",
                        "NO_REWARD_AVAILABLE",
                    }:
                        unavailable_frames += 1
                    else:
                        unavailable_frames = 0
                    if unavailable_frames >= 3:
                        self._set_task_attention(
                            task_id,
                            "NO_CLAIMABLE_REWARD_UNVERIFIED",
                            "Worker found a stable in-world state but has no verified unavailable-reward signal",
                        )
                        return
            time.sleep(
                min(
                    self.settings.gameplay_poll_interval_seconds,
                    max(0, deadline - time.monotonic()),
                )
            )
        self._set_task_attention(
            task_id,
            "DAILY_GIFT_CONFIRMATION_TIMEOUT",
            "No DAILY_GIFT_CONFIRMED result arrived before the bounded task deadline",
        )

    def _verified_no_reward(self, snapshot, task, evidence) -> bool:
        observation = snapshot.get("observation")
        observed_at = snapshot.get("last_observation_at")
        if not isinstance(evidence, dict) or not self._safe_observation(observation):
            return False
        try:
            identity = float(evidence.get("identity_confidence", 0))
        except (TypeError, ValueError, OverflowError):
            return False
        return bool(
            snapshot["telemetry"].get("daily_gift_state") == "NO_REWARD_AVAILABLE"
            and observation.get("screen") == "IN_WORLD_IDLE"
            and observed_at is not None
            and ensure_utc(task.started_at) <= ensure_utc(observed_at) <= utcnow()
            and (utcnow() - ensure_utc(observed_at)).total_seconds() <= 10
            and evidence.get("semantic") == "NO_REWARD_AVAILABLE"
            and evidence.get("availability") == "NO_REWARD_AVAILABLE"
            and evidence.get("icon_state") == "INACTIVE"
            and evidence.get("icon_present") is True
            and evidence.get("evidence_frame_id") == observation.get("source_frame_id")
            and (
                identity >= 0.94
                or (identity >= 0.85 and evidence.get("hover_verified") is True)
            )
        )

    def _disable_worker(self, control, job: Job, *, timeout: float) -> None:
        command_id = self._request_worker_mode(control, job, "DISABLED")
        deadline = time.monotonic() + min(timeout, 30)
        while time.monotonic() < deadline:
            snapshot = self._worker_snapshot(job.runtime_id)
            if (
                snapshot
                and snapshot["mode"] == "DISABLED"
                and snapshot["state"] == "DISABLED"
                and snapshot["held_inputs"] is False
            ):
                with self.db.session() as session:
                    command = session.get(WorkerCommand, command_id)
                    if command is not None and command.status == "COMPLETED":
                        return
            time.sleep(
                min(
                    self.settings.gameplay_poll_interval_seconds,
                    max(0, deadline - time.monotonic()),
                )
            )
        raise TimeoutError("GameWorker disable and input release were not confirmed")

    def _managed_task_stop(self, job: Job) -> None:
        observed = self.provider.inspect(self._descriptor(job.runtime_id))
        if observed.state == RuntimeState.STOPPED:
            return
        with self.db.transaction(immediate=True) as session:
            runtime = session.get(RuntimeInstance, job.runtime_id)
            if runtime is not None:
                runtime.desired_state = DesiredState.STOPPED
        self._stop(job)

    def _reconcile_terminal_task(self, job: Job, task: GameplayTask) -> None:
        """After restart, clean up a committed result without replaying gameplay."""
        try:
            control = WorkerControlService(self.db)
            snapshot = self._worker_snapshot(job.runtime_id)
            if snapshot and snapshot["mode"] != "DISABLED":
                self._disable_worker(
                    control,
                    job,
                    timeout=self.settings.gameplay_observation_timeout_seconds,
                )
            if job.payload.get("owns_runtime"):
                self._managed_task_stop(job)
        except Exception as exc:  # noqa: BLE001 - keep terminal gameplay result intact
            self._record_cleanup_error(
                task.id, f"terminal task cleanup failed: {type(exc).__name__}: {exc}"
            )

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
            str(
                job.payload.get("image_version") or self.settings.current_image_version
            ),
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
            elif job.kind in {JobKind.DAILY_GIFT_CLAIM, JobKind.LONG_SESSION}:
                # Gameplay failures belong to GameplayTask. Preserve observed
                # runtime state; managed cleanup records its own failure there.
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
