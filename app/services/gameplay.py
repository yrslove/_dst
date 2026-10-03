from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountScheduleState,
    GameplayTask,
    GameplayTaskStatus,
    Job,
    JobStatus,
    WorkerRun,
    utcnow,
)

DAILY_GIFT_TASK = "DAILY_GIFT_CLAIM"
INWORLD_WEEKLY_CLAIM_TASK = "INWORLD_WEEKLY_CLAIM"
CONFIRMED_SEMANTIC = "DAILY_GIFT_CONFIRMED"
INWORLD_CONFIRMED_SEMANTIC = "IN_WORLD_GIFT_CONFIRMED"
ACTIVE_TASK_STATUSES = (
    GameplayTaskStatus.PENDING,
    GameplayTaskStatus.RUNNING,
)
GIFT_PROGRESS_STATES = {
    "GIFT_AVAILABLE",
    "GIFT_INTERACTION_STARTED",
    "GIFT_UI_OPEN",
    "GIFT_UI_CLOSED",
    CONFIRMED_SEMANTIC,
}


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=utcnow().tzinfo)


def _confirmation_parts(value: object) -> tuple[str, dict] | None:
    if not isinstance(value, dict) or value.get("semantic") != CONFIRMED_SEMANTIC:
        return None
    action_id = value.get("action_id")
    frame_id = value.get("evidence_frame_id")
    sequence = value.get("evidence_sequence")
    observed_at = value.get("observed_at")
    if (
        not isinstance(action_id, str)
        or not action_id
        or len(action_id) > 256
        or not isinstance(frame_id, str)
        or not frame_id
        or len(frame_id) > 256
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        or _parse_timestamp(observed_at) is None
    ):
        return None
    identity = json.dumps([DAILY_GIFT_TASK, action_id, frame_id], separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest(), {
        "semantic": CONFIRMED_SEMANTIC,
        "action_id": action_id,
        "evidence_frame_id": frame_id,
        "evidence_sequence": sequence,
        "observed_at": observed_at,
    }


def inworld_claim_identity(action_id: str, received_frame_id: str) -> str:
    return json.dumps(
        [INWORLD_WEEKLY_CLAIM_TASK, action_id, received_frame_id],
        separators=(",", ":"),
    )


def _inworld_confirmation_parts(value: object, runtime_id: int):
    if not isinstance(value, dict) or value.get("semantic") != INWORLD_CONFIRMED_SEMANTIC:
        return None
    action_id = value.get("action_id")
    frame_id = value.get("evidence_frame_id")
    received_frame_id = value.get("received_frame_id")
    sequence = value.get("evidence_sequence")
    observed_at = value.get("observed_at")
    backend = value.get("backend")
    item_id = value.get("item_id")
    native = value.get("verification") == "NATIVE_ACK_AFTER_CANONICAL_CLICK"
    if native:
        attempted = _parse_timestamp(value.get("claim_attempt_at"))
        observed = _parse_timestamp(observed_at)
        modified = backend.get("modified") if isinstance(backend, dict) else None
        if not (
            value.get("active_gift_icon_disappeared") is True
            and value.get("visual_state") == "IN_WORLD_IDLE"
            and attempted is not None and observed is not None
            and attempted <= observed
            and isinstance(modified, (int, float)) and not isinstance(modified, bool)
            and math.isfinite(modified)
            and attempted.timestamp() <= modified <= observed.timestamp() + 60
        ):
            return None
    if (
        not isinstance(action_id, str)
        or not action_id
        or len(action_id) > 256
        or not isinstance(frame_id, str)
        or not frame_id
        or len(frame_id) > 256
        or (not native and (not isinstance(received_frame_id, str)
                            or not received_frame_id or len(received_frame_id) > 256))
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        or _parse_timestamp(observed_at) is None
        or value.get("runtime_id") != runtime_id
        or (not native and value.get("verification") != "RECORDED_CANONICAL_CLOSE_FRESH_WORLD")
        or not isinstance(item_id, int)
        or isinstance(item_id, bool)
        or item_id < 1
        or not isinstance(backend, dict)
        or backend.get("operation") != "SetItemOpened_Complete"
        or backend.get("http_status") != 200
        or backend.get("error") is not False
        or not isinstance(backend.get("ack_sha256"), str)
        or len(backend["ack_sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in backend["ack_sha256"])
    ):
        return None
    identity = inworld_claim_identity(action_id, frame_id if native else received_frame_id)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest(), {
        "semantic": INWORLD_CONFIRMED_SEMANTIC,
        "action_id": action_id,
        "received_frame_id": received_frame_id,
        "verification": value.get("verification"),
        "evidence_frame_id": frame_id,
        "evidence_sequence": sequence,
        "observed_at": observed_at,
        "item_id": item_id,
        "backend_operation": backend["operation"],
        "backend_http_status": 200,
        "backend_ack_sha256": backend["ack_sha256"],
    }


def persist_worker_inworld_gift_confirmation(
    session: Session,
    *,
    account_id: int,
    runtime_id: int,
    worker_run: WorkerRun | None,
    report: dict,
    now: datetime,
    sqlite: bool,
) -> GameplayTask | None:
    """Persist verified claim receipts and fresh claimability in the heartbeat transaction."""
    telemetry = report.get("telemetry")
    confirmation = (
        _inworld_confirmation_parts(telemetry.get("inworld_gift_confirmation"), runtime_id)
        if isinstance(telemetry, dict)
        else None
    )
    if not isinstance(telemetry, dict):
        return None
    statement = select(AccountScheduleState).where(
        AccountScheduleState.account_id == account_id
    )
    if not sqlite:
        statement = statement.with_for_update()
    state = session.scalar(statement)
    if state is None:
        return None
    changed = False
    observed_at = _parse_timestamp(report.get("last_observation_at"))
    fresh = observed_at is not None and -5 <= (now - observed_at).total_seconds() <= 180
    availability = telemetry.get("inworld_gift_state")
    if confirmation is None:
        if fresh and availability == "IN_WORLD_GIFT_ACTIONABLE":
            if not state.pending_gift:
                state.pending_gift = True
                changed = True
        elif fresh and availability in {
            "IN_WORLD_GIFT_PENDING",
            "NO_REWARD_AVAILABLE",
        }:
            if state.pending_gift:
                state.pending_gift = False
                changed = True
            if state.next_weekly_eligible_at is not None or state.estimated_due_at is not None:
                state.next_weekly_eligible_at = None
                state.estimated_due_at = None
                changed = True
        if changed:
            _cancel_obsolete_queued_schedule_job(session, state, now)
            state.schedule_revision += 1
        return None

    confirmation_key, result = confirmation
    existing = session.scalar(
        select(GameplayTask).where(
            GameplayTask.account_id == account_id,
            GameplayTask.kind == INWORLD_WEEKLY_CLAIM_TASK,
            GameplayTask.confirmation_key == confirmation_key,
        )
    )
    if existing is not None:
        if state.pending_gift:
            state.pending_gift = False
            state.schedule_revision += 1
        return existing
    if worker_run is not None and worker_run.id is None:
        session.flush()
    confirmed_at = _parse_timestamp(result["observed_at"]) or now
    receipt = GameplayTask(
        account_id=account_id,
        runtime_id=runtime_id,
        worker_run_id=worker_run.id if worker_run else None,
        kind=INWORLD_WEEKLY_CLAIM_TASK,
        status=GameplayTaskStatus.SUCCEEDED,
        started_at=confirmed_at,
        updated_at=now,
        completed_at=now,
        confirmation_key=confirmation_key,
        claim_confirmed_at=confirmed_at,
        claim_persisted_at=now,
        result_json=result,
    )
    session.add(receipt)
    if state.weekly_state == "SYNCED":
        state.weekly_collected += 1
        if state.weekly_collected >= state.weekly_target:
            state.phase = "DONE"
        elif state.weekly_target - state.weekly_collected == 1:
            state.phase = "FINAL_COLLECTION"
        else:
            state.phase = "FARMING"
    else:
        state.confirmed_claims_current_observation += 1
    state.pending_gift = False
    state.last_weekly_claim_at = confirmed_at
    state.next_weekly_eligible_at = None
    state.estimated_due_at = None
    _cancel_obsolete_queued_schedule_job(session, state, now)
    state.schedule_revision += 1
    session.flush()
    return receipt


def _cancel_obsolete_queued_schedule_job(session, state, now: datetime) -> None:
    if not state.active_job_key:
        return
    job = session.scalar(
        select(Job).where(Job.idempotency_key == state.active_job_key)
    )
    if job is None or job.status not in {JobStatus.PENDING, JobStatus.RETRY}:
        return
    intent = job.payload.get("account_schedule_intent")
    weekly_complete = (
        state.weekly_state == "SYNCED"
        and state.weekly_collected >= state.weekly_target
    )
    obsolete = (
        state.pending_gift and intent in {"WEEKLY_FARM", "FINAL_COLLECTION"}
    ) or (
        state.weekly_state != "SYNCED"
        and intent in {"WEEKLY_FARM", "FINAL_COLLECTION"}
    ) or (
        weekly_complete and intent in {"WEEKLY_FARM", "FINAL_COLLECTION"}
    ) or (not state.pending_gift and intent == "CLAIM_PENDING_GIFT") or (
        state.daily_status != "PENDING" and intent == "DAILY_MAINTENANCE"
    )
    if not obsolete:
        return
    job.status = JobStatus.CANCELLED
    job.completed_at = now
    task_id = job.payload.get("gameplay_task_id")
    task = session.get(GameplayTask, task_id) if task_id else None
    if task is not None and task.status in {
        GameplayTaskStatus.PENDING,
        GameplayTaskStatus.RUNNING,
    }:
        task.status = GameplayTaskStatus.CANCELLED
        task.completed_at = now
        task.updated_at = now
    state.active_job_key = None
    state.active_job_type = None


def _active_task(
    session: Session, account_id: int, *, lock: bool
) -> GameplayTask | None:
    statement = (
        select(GameplayTask)
        .where(
            GameplayTask.account_id == account_id,
            GameplayTask.kind == DAILY_GIFT_TASK,
            GameplayTask.status.in_(ACTIVE_TASK_STATUSES),
        )
        .order_by(GameplayTask.id.desc())
        .limit(1)
    )
    if lock:
        statement = statement.with_for_update()
    return session.scalar(statement)


def persist_worker_gift_progress(
    session: Session,
    *,
    account_id: int,
    runtime_id: int,
    worker_run: WorkerRun | None,
    report: dict,
    worker_state: str,
    now: datetime,
    sqlite: bool,
) -> GameplayTask | None:
    """Persist gift progress/result inside the enclosing runtime-heartbeat transaction."""
    telemetry = report.get("telemetry")
    if not isinstance(telemetry, dict):
        telemetry = {}
    gift_state = telemetry.get("daily_gift_state")
    confirmation = _confirmation_parts(telemetry.get("daily_gift_confirmation"))
    confirmation_key = confirmation[0] if confirmation else None
    result = confirmation[1] if confirmation else None

    if confirmation_key is not None:
        existing = session.scalar(
            select(GameplayTask).where(
                GameplayTask.account_id == account_id,
                GameplayTask.kind == DAILY_GIFT_TASK,
                GameplayTask.confirmation_key == confirmation_key,
            )
        )
        if existing is not None:
            return existing

    task = _active_task(session, account_id, lock=not sqlite)
    observed_at = _parse_timestamp(report.get("last_observation_at")) or now
    if confirmation_key is not None and result is not None:
        if worker_run is not None and worker_run.id is None:
            session.flush()
        confirmed_at = _parse_timestamp(result["observed_at"])
        if task is None:
            task = GameplayTask(
                account_id=account_id,
                runtime_id=runtime_id,
                worker_run_id=worker_run.id if worker_run else None,
                kind=DAILY_GIFT_TASK,
                status=GameplayTaskStatus.SUCCEEDED,
                started_at=observed_at,
                updated_at=now,
                completed_at=now,
                confirmation_key=confirmation_key,
                claim_confirmed_at=confirmed_at,
                claim_persisted_at=now,
                result_json=result,
            )
            session.add(task)
            return task
        if task.status in {
            GameplayTaskStatus.PENDING,
            GameplayTaskStatus.RUNNING,
            GameplayTaskStatus.NEEDS_ATTENTION,
        }:
            task.runtime_id = runtime_id
            task.worker_run_id = worker_run.id if worker_run else task.worker_run_id
            task.status = GameplayTaskStatus.SUCCEEDED
            task.updated_at = now
            task.completed_at = now
            task.confirmation_key = confirmation_key
            task.claim_confirmed_at = confirmed_at
            task.claim_persisted_at = now
            task.result_json = result
            task.error_code = None
            task.error_message = None
            return task

    # NEEDS_ATTENTION is an operator-visible terminal decision for this run.
    # Routine cleanup heartbeats must not reopen it; a verified confirmation above
    # is the sole allowed reconciliation to success.
    if task is not None and task.status == GameplayTaskStatus.NEEDS_ATTENTION:
        return task

    if gift_state in GIFT_PROGRESS_STATES:
        if worker_run is not None and worker_run.id is None:
            session.flush()
        if task is None:
            initial_status = (
                GameplayTaskStatus.NEEDS_ATTENTION
                if gift_state in {"GIFT_UI_CLOSED", CONFIRMED_SEMANTIC}
                else GameplayTaskStatus.RUNNING
            )
            task = GameplayTask(
                account_id=account_id,
                runtime_id=runtime_id,
                worker_run_id=worker_run.id if worker_run else None,
                kind=DAILY_GIFT_TASK,
                status=initial_status,
                started_at=observed_at,
                updated_at=now,
                error_code=(
                    "GIFT_CLAIM_UNCONFIRMED"
                    if initial_status == GameplayTaskStatus.NEEDS_ATTENTION
                    else None
                ),
                error_message=(
                    "Worker reported gift UI closed without a valid confirmation"
                    if initial_status == GameplayTaskStatus.NEEDS_ATTENTION
                    else None
                ),
            )
            session.add(task)
        else:
            task.runtime_id = runtime_id
            task.worker_run_id = worker_run.id if worker_run else task.worker_run_id
            task.updated_at = now
            if gift_state in {
                "GIFT_AVAILABLE",
                "GIFT_INTERACTION_STARTED",
                "GIFT_UI_OPEN",
            }:
                task.status = GameplayTaskStatus.RUNNING
                task.error_code = None
                task.error_message = None
            elif gift_state == "GIFT_UI_CLOSED":
                task.status = GameplayTaskStatus.NEEDS_ATTENTION
                task.error_code = "GIFT_CLAIM_UNCONFIRMED"
                task.error_message = (
                    "Worker reported gift UI closed without a valid confirmation"
                )
            elif gift_state == CONFIRMED_SEMANTIC:
                task.status = GameplayTaskStatus.NEEDS_ATTENTION
                task.error_code = "INVALID_GIFT_CONFIRMATION"
                task.error_message = "Worker confirmation payload was incomplete"

    if (
        task is not None
        and task.status in ACTIVE_TASK_STATUSES
        and worker_run is not None
        and task.worker_run_id == worker_run.id
    ):
        if worker_state == "ERROR":
            task.status = GameplayTaskStatus.FAILED
            task.completed_at = now
            task.error_code = str(report.get("error_code") or "WORKER_ERROR")[:80]
            task.error_message = "Worker ended with an error before confirmation"
        elif worker_state == "NEEDS_ATTENTION":
            task.status = GameplayTaskStatus.NEEDS_ATTENTION
            task.error_code = str(report.get("error_code") or "WORKER_ATTENTION")[:80]
            task.error_message = "Worker requires attention before confirmation"
        elif worker_state in {"DISABLED", "STOPPED"}:
            task.status = GameplayTaskStatus.NEEDS_ATTENTION
            task.error_code = "WORKER_RUN_INTERRUPTED"
            task.error_message = "Worker run ended before confirmation"
        task.updated_at = now

    return task
