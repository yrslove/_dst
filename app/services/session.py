"""Bounded idle sessions using the existing executor, worker and task records."""

from __future__ import annotations

import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.models import GameplayTask, GameplayTaskStatus, RuntimeState, WorkerRun, utcnow
from app.runtime.world_profile import desired_profile_hash
from app.services.records import add_event
from app.services.workers import WorkerControlService


def utc(value):
    return value if value.tzinfo else value.replace(tzinfo=utcnow().tzinfo)


def session_failure(snapshot, now, *, max_age=20):
    if not snapshot:
        return "WORKER_UNAVAILABLE"
    if (
        not snapshot["healthy"]
        or (utc(now) - utc(snapshot["updated_at"])).total_seconds() > max_age
    ):
        return "RUNTIME_UNHEALTHY"
    if snapshot["state"] in {"ERROR", "NEEDS_ATTENTION"}:
        return "WORKER_FAILED"
    if (
        not snapshot.get("steam_running", True)
        or not snapshot.get("dst_running", True)
        or snapshot.get("phase", "GAME_READY") != "GAME_READY"
    ):
        return "RUNTIME_PROCESS_FAILED"
    screen = (snapshot.get("observation") or {}).get("screen")
    if screen in {"DEAD", "WORLD_RESET_PENDING", "RESET_PENDING"}:
        return "GAMEPLAY_" + screen
    if snapshot.get("held_inputs") is True:
        return "INPUT_NOT_RELEASED"
    return None


def safe_world_profile_failure(snapshot, runtime, now, *, max_age=20):
    """Require fresh profile evidence bound to this runtime and DST process."""
    evidence = snapshot.get("world_profile") if snapshot else None
    if not isinstance(evidence, dict) or evidence.get("status") != "VERIFIED":
        return "SAFE_WORLD_PROFILE_UNVERIFIED"
    expected_hash = desired_profile_hash()
    if (
        evidence.get("enabled") is False
        or evidence.get("verification_scope") != "CONFIG_FILE_AND_PROCESS"
        or evidence.get("profile_layer") != "worldgenoverride.lua"
        or evidence.get("configuration_verified") is not True
        or evidence.get("account_id") != runtime.account_id
        or evidence.get("runtime_id") != runtime.id
        or evidence.get("runtime_generation") != runtime.runtime_generation
        or evidence.get("desired_profile_hash") != expected_hash
        or evidence.get("applied_profile_hash") != expected_hash
    ):
        return "SAFE_WORLD_PROFILE_UNVERIFIED"
    world_path = evidence.get("world_path")
    if (
        not isinstance(world_path, str)
        or Path(world_path).name != "worldgenoverride.lua"
        or Path(world_path).parent.name != "Cluster_1"
    ):
        return "SAFE_WORLD_PROFILE_UNVERIFIED"
    process_id = evidence.get("process_id")
    start_ticks = evidence.get("process_start_ticks")
    if not isinstance(process_id, int) or process_id < 2:
        return "SAFE_WORLD_PROFILE_UNVERIFIED"
    if not isinstance(start_ticks, int) or start_ticks <= 0:
        return "SAFE_WORLD_PROFILE_UNVERIFIED"
    generation = (
        f"r{runtime.id}-g{runtime.runtime_generation}-p{process_id}-t{start_ticks}"
    )
    if evidence.get("process_generation") != generation:
        return "SAFE_WORLD_PROFILE_UNVERIFIED"
    try:
        process_started = utc(datetime.fromisoformat(evidence["process_started_at"]))
        verified_at = utc(datetime.fromisoformat(evidence["verified_at"]))
    except (KeyError, TypeError, ValueError):
        return "SAFE_WORLD_PROFILE_UNVERIFIED"
    age = (utc(now) - verified_at).total_seconds()
    if (
        verified_at < process_started
        or age < -5
        or age > max_age
        or not snapshot.get("dst_running")
    ):
        return "SAFE_WORLD_PROFILE_UNVERIFIED"
    return None


class LongSessionMixin:
    def _session_update(
        self, job, task_id, progress, *, status=None, reason=None, event=None
    ):
        with self.db.transaction(immediate=True) as session:
            self._assert_owned(session, job)
            task = session.get(GameplayTask, task_id)
            if task is None or task.status not in {"PENDING", "RUNNING"}:
                return
            task.result_json = dict(progress)
            task.updated_at = utcnow()
            if status:
                task.status = status
            if reason:
                task.error_code = None if status == "COMPLETED" else reason
                task.completed_at = utcnow()
                task.result_json = {**progress, "terminal_reason": reason}
            if event:
                add_event(
                    session,
                    level="INFO",
                    kind="gameplay.session",
                    message=event,
                    account_id=job.account_id,
                    runtime_id=job.runtime_id,
                    metadata={
                        "task_id": task_id,
                        "state": progress.get("state"),
                        "reason": reason,
                    },
                )

    def _long_session(self, job):
        task_id = job.payload["gameplay_task_id"]
        task = self._gameplay_task(task_id)
        if (
            task is None
            or task.kind != "LONG_SESSION"
            or (task.account_id, task.runtime_id) != (job.account_id, job.runtime_id)
        ):
            raise ValueError("session task ownership mismatch")
        if task.status not in {"PENDING", "RUNNING"}:
            self._reconcile_terminal_task(job, task)
            return
        control = WorkerControlService(self.db)
        worker_started = False
        owned = job.payload.get("owns_runtime", False)
        progress = dict(
            task.result_json
            or {
                "requested_duration": job.payload["requested_duration"],
                "state": "PREPARING",
                "checkpoints": 0,
                "recoveries": 0,
                "gift_transitions": [],
                "confirmations": [],
            }
        )
        resumed = task.status == "RUNNING"
        reason = None
        try:
            if (
                shutil.disk_usage(self.settings.session_storage_path).free
                < self.settings.session_min_free_bytes
            ):
                reason = "STORAGE_GUARD_TRIGGERED"
                return
            if not self.settings.runtime_safe_idle_world:
                reason = "SAFE_WORLD_PROFILE_REQUIRED"
                return
            runtime = self._runtime_for_task(job.runtime_id)
            if runtime is None or runtime.verified_at is None:
                reason = "RUNTIME_NOT_VERIFIED"
                return
            observed = self.provider.inspect(self._descriptor(job.runtime_id))
            if resumed:
                snap = self._worker_snapshot(job.runtime_id)
                with self.db.session() as session:
                    run = (
                        session.get(WorkerRun, task.worker_run_id)
                        if task.worker_run_id
                        else None
                    )
                    owner_ok = run is not None and run.ended_at is None
                if (
                    observed.state != RuntimeState.RUNNING
                    or not owner_ok
                    or not snap
                    or snap["mode"] != "ACTIVE"
                ):
                    reason = "SESSION_RESTART_UNRECONCILED"
                    return
                reason = safe_world_profile_failure(snap, runtime, utcnow())
                if reason:
                    return
                progress["safe_world_profile"] = {
                    "desired_profile_hash": snap["world_profile"][
                        "desired_profile_hash"
                    ],
                    "applied_profile_hash": snap["world_profile"][
                        "applied_profile_hash"
                    ],
                    "process_generation": snap["world_profile"][
                        "process_generation"
                    ],
                    "verified_at": snap["world_profile"]["verified_at"],
                }
                worker_started = True
                command = self._latest_active_mode_command(job.runtime_id)
                fresh = (
                    self._wait_for_fresh_observation(
                        job.runtime_id,
                        after=utcnow(),
                        timeout=40,
                        mode="ACTIVE",
                        command_id=command,
                    )
                    if command
                    else None
                )
                if (
                    not self._safe_observation(fresh)
                    or fresh.get("screen") != "IN_WORLD_IDLE"
                ):
                    reason = "SESSION_RESTART_UNSAFE"
                    return
            else:
                snap = self._worker_snapshot(job.runtime_id)
                if snap and (
                    snap["mode"] != "DISABLED" or snap.get("held_inputs") is not False
                ):
                    reason = "INPUT_OWNERSHIP_ACTIVE"
                    return
                owned = observed.state == RuntimeState.STOPPED
                self._persist_task_job_payload(job, {"owns_runtime": owned})
                start = utcnow()
                if owned:
                    self._start(job)
                elif observed.state != RuntimeState.RUNNING:
                    reason = "RUNTIME_STATE_UNSAFE"
                    return
                if not self._wait_for_game_ready(
                    job.runtime_id,
                    after=start,
                    timeout=self.settings.gameplay_readiness_timeout_seconds,
                ):
                    reason = "GAME_READY_TIMEOUT"
                    return
                snap = self._worker_snapshot(job.runtime_id)
                reason = safe_world_profile_failure(snap, runtime, utcnow())
                if reason:
                    return
                progress["safe_world_profile"] = {
                    "desired_profile_hash": snap["world_profile"][
                        "desired_profile_hash"
                    ],
                    "applied_profile_hash": snap["world_profile"][
                        "applied_profile_hash"
                    ],
                    "process_generation": snap["world_profile"][
                        "process_generation"
                    ],
                    "verified_at": snap["world_profile"]["verified_at"],
                }
                command = self._request_worker_mode(control, job, "OBSERVE")
                worker_started = True
                fresh = self._wait_for_fresh_observation(
                    job.runtime_id,
                    after=utcnow(),
                    timeout=40,
                    mode="OBSERVE",
                    command_id=command,
                )
                if not self._safe_observation(fresh):
                    reason = "ENTRY_OBSERVATION_UNSAFE"
                    return
                self._mark_task_running(task_id)
                self._request_worker_mode(control, job, "ACTIVE")
                progress["entry_deadline"] = (
                    utcnow() + timedelta(seconds=300)
                ).isoformat()
            self._attach_worker_run(task_id, job.runtime_id)
            self._session_update(
                job, task_id, progress, event="session monitoring started"
            )
            last_frame = None
            unknown = 0
            while True:
                now = utcnow()
                if not self.jobs.owns(job):
                    # A newer executor owns reconciliation; never send stale cleanup commands.
                    worker_started = owned = False
                    return
                free = shutil.disk_usage(self.settings.session_storage_path).free
                progress["free_bytes"] = free
                if free < self.settings.session_min_free_bytes:
                    reason = "STORAGE_GUARD_TRIGGERED"
                    break
                snap = self._worker_snapshot(job.runtime_id)
                reason = session_failure(snap, now)
                if reason:
                    break
                reason = safe_world_profile_failure(snap, runtime, now)
                if reason:
                    break
                obs = snap.get("observation") or {}
                stamp = snap.get("last_observation_at")
                if not stamp or (now - utc(stamp)).total_seconds() > 40:
                    reason = "PERCEPTION_STALE"
                    break
                frame = obs.get("source_frame_id")
                if frame != last_frame:
                    last_frame = frame
                    safe = (
                        self._safe_observation(obs)
                        and obs.get("screen") == "IN_WORLD_IDLE"
                    )
                    if safe and 0 <= (now - utc(stamp)).total_seconds() <= 20:
                        unknown = 0
                        if "monitor_started_at" not in progress:
                            self._attach_worker_run(task_id, job.runtime_id)
                            progress["monitor_started_at"] = utc(stamp).isoformat()
                            progress["deadline"] = (
                                utc(stamp)
                                + timedelta(seconds=progress["requested_duration"])
                            ).isoformat()
                        progress.update(
                            state="IN_WORLD",
                            last_verified_in_world_at=utc(stamp).isoformat(),
                            last_frame_id=frame,
                            checkpoints=progress["checkpoints"] + 1,
                        )
                        progress["gift_icon_evidence"] = snap["telemetry"].get(
                            "gift_availability_evidence"
                        )
                        gift = snap["telemetry"].get(
                            "daily_gift_state", "GIFT_AVAILABILITY_UNKNOWN"
                        )
                        event = None
                        changes = progress["gift_transitions"]
                        if not changes or changes[-1]["state"] != gift:
                            changes.append(
                                {"state": gift, "at": utc(stamp).isoformat()}
                            )
                            progress["gift_transitions"] = changes[-32:]
                            event = "gift availability changed: " + gift
                        confirmed = snap["telemetry"].get("daily_gift_confirmation")
                        if (
                            isinstance(confirmed, dict)
                            and confirmed.get("semantic") == "DAILY_GIFT_CONFIRMED"
                            and confirmed not in progress["confirmations"]
                        ):
                            progress["confirmations"] = (
                                progress["confirmations"] + [confirmed]
                            )[-16:]
                            event = "verified daily gift confirmation observed"
                        self._session_update(job, task_id, progress, event=event)
                        if utc(stamp) >= datetime.fromisoformat(progress["deadline"]):
                            reason = "TIME_BUDGET_REACHED"
                            break
                    elif "monitor_started_at" in progress:
                        unknown += 1
                        if unknown >= 3:
                            reason = (
                                "PERCEPTION_UNKNOWN"
                                if obs.get("screen") == "UNKNOWN"
                                else "SESSION_NOT_CONTROLLABLE"
                            )
                            break
                    elif now >= datetime.fromisoformat(progress["entry_deadline"]):
                        reason = "WORLD_ENTRY_TIMEOUT"
                        break
                time.sleep(2)
        except Exception as exc:  # noqa: BLE001 - persist failure and guarantee cleanup
            reason = "SESSION_EXCEPTION"
            progress["failure_detail"] = f"{type(exc).__name__}: {exc}"[:1000]
        finally:
            if reason:
                status = (
                    "COMPLETED"
                    if reason == "TIME_BUDGET_REACHED"
                    else "NEEDS_ATTENTION"
                )
                progress["state"] = status
                self._session_update(
                    job,
                    task_id,
                    progress,
                    status=status,
                    reason=reason,
                    event="session terminal: " + reason,
                )
            error = None
            if worker_started:
                try:
                    self._disable_worker(control, job, timeout=40)
                except Exception as exc:  # noqa: BLE001 - persist failure and guarantee cleanup
                    error = "worker cleanup: " + str(exc)
            if owned:
                try:
                    self._managed_task_stop(job)
                except Exception as exc:  # noqa: BLE001 - persist failure and guarantee cleanup
                    error = (error or "") + "; runtime cleanup: " + str(exc)
            if error:
                self._record_cleanup_error(task_id, error)
                self._finish_gameplay_task(
                    task_id, GameplayTaskStatus.NEEDS_ATTENTION, "CLEANUP_FAILED", error
                )
