from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GameplayTask, GameplayTaskStatus, WorkerRun, utcnow

DAILY_GIFT_TASK = "DAILY_GIFT_CLAIM"
CONFIRMED_SEMANTIC = "DAILY_GIFT_CONFIRMED"
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
