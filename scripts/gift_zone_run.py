"""Persistent observation run; gameplay remains owned by existing GameWorkers."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv

from runtime_agent.gameworker.reward_evidence import durable_json
from scripts.dual_ab import API, append_jsonl, worker_report

GUESTS = {1: "dst-000001-g1", 2: "dst-000002-g2"}


def stamp():
    return datetime.now(UTC).isoformat()


def age(value):
    return (datetime.now(UTC) - datetime.fromisoformat(value)).total_seconds() if value else 999999.0


def collect_events(root, meta, account):
    entry = meta["accounts"][str(account)]
    path = f'/home/dst/.local/state/dst-runtime/experiments/{entry["session_id"]}/events.jsonl'
    # Read bounded increments; no screenshots/video and no full-file polling.
    code = "import pathlib,sys;p=pathlib.Path(sys.argv[1]);f=p.open('rb') if p.exists() else None;f and f.seek(int(sys.argv[2]));sys.stdout.buffer.write(f.read(1048576) if f else b'')"
    result = subprocess.run(["incus", "exec", GUESTS[account], "--", "python3", "-c", code,
                             path, str(entry.get("event_offset", 0))],
                            capture_output=True, timeout=20, check=True)
    raw = result.stdout
    raw = raw[:raw.rfind(b"\n") + 1] if raw else b""
    with (root / f"account-{account}-events.jsonl").open("ab") as stream:
        stream.write(raw)
    entry["event_offset"] = entry.get("event_offset", 0) + len(raw)


def finish_report(root, meta):
    report = {"run_id": meta["run_id"], "status": meta["status"],
              "run_started_at": meta.get("run_started_at"),
              "planned_end_at": meta.get("planned_end_at"), "ended_at": stamp(),
              "historical_collected": "UNKNOWN", "accounts": {},
              "interpretation": "GREY -> ACTIVE -> collection is an observed sequence, not proof that micro-movement causes activation."}
    for a in GUESTS:
        path = root / f"account-{a}-events.jsonl"
        events = [json.loads(v) for v in path.read_text().splitlines()] if path.exists() else []
        events = list({(e["timestamp"], e.get("session_id")): e for e in events}.values())
        successes, attempts, failures, latencies = {}, {}, set(), []
        grey_at, previous_gift, grey_count, transitions = None, None, 0, 0
        drift, stuck, recoveries = 0, 0, 0
        for e in events:
            gift = e.get("gift", {})
            state = gift.get("availability")
            grey = gift.get("icon_present") and state in {"GIFT_DISABLED_OR_UNAVAILABLE", "IN_WORLD_GIFT_PENDING"}
            if grey and previous_gift != "GREY":
                grey_count += 1
                grey_at = gift.get("observed_at") or e["timestamp"]
            if state == "GIFT_AVAILABLE" and previous_gift != "ACTIVE" and grey_at:
                transitions += 1
                latencies.append((datetime.fromisoformat(gift.get("observed_at") or e["timestamp"]) - datetime.fromisoformat(grey_at)).total_seconds())
                grey_at = None
            previous_gift = "GREY" if grey else "ACTIVE" if state == "GIFT_AVAILABLE" else "NONE"
            if previous_gift == "NONE" and e.get("screen") == "IN_WORLD_IDLE":
                grey_at = None
            attempt = e.get("attempt") or {}
            key = attempt.get("claim_started_at")
            if key:
                attempts[key] = attempt
                if attempt.get("status") in {"TIMED_OUT", "FAILED", "SAFETY_BLOCKED", "CANCELLED"}:
                    failures.add(key)
            receipt = e.get("receipt") or {}
            if receipt.get("active_gift_icon_disappeared") is True and receipt.get("backend", {}).get("http_status") == 200:
                successes[str(receipt.get("item_id"))] = receipt.get("observed_at") or receipt.get("claim_timestamp")
            zone = e.get("movement", {}).get("station_zone", {})
            displacement = zone.get("displacement")
            if displacement and (abs(displacement[0]) > 32 or abs(displacement[1]) > 20):
                drift += 1
            stuck = max(stuck, e.get("movement", {}).get("movement_failures", 0))
            recoveries = max(recoveries, e.get("recoveries", 0))
        times = sorted(t for t in successes.values() if t)
        report["accounts"][str(a)] = {
            "total_successful_gifts": len(successes), "success_timestamps": times,
            "grey_detection_episodes": grey_count, "grey_to_active_transitions": transitions,
            "collection_attempts": len(attempts), "collection_failures": len(failures),
            "grey_to_active_latency_seconds": {"min": min(latencies), "median": statistics.median(latencies), "max": max(latencies)} if latencies else None,
            "success_intervals_seconds": [(datetime.fromisoformat(y)-datetime.fromisoformat(x)).total_seconds() for x,y in pairwise(times)],
            "outside_boundary_observations": drift, "max_consecutive_stuck_movements": stuck,
            "recoveries": recoveries,
            "health_events": meta["accounts"][str(a)].get("health_events", []),
            "worker_restarts": max([h.get("signature", [None]*4)[3] or 0 for h in meta["accounts"][str(a)].get("health_events", []) if h.get("signature")] or [0]),
            "worker_crash_or_attention_events": sum(h.get("signature", [None])[0] in {"ERROR", "NEEDS_ATTENTION"} for h in meta["accounts"][str(a)].get("health_events", []) if h.get("signature")),
        }
    observed = sum(a["grey_to_active_transitions"] for a in report["accounts"].values())
    successes = sum(a["total_successful_gifts"] for a in report["accounts"].values())
    report["hypothesis_assessment"] = (
        f"Observed {observed} GREY-to-ACTIVE transitions and {successes} confirmed collections while using bounded gift-zone movement. This supports compatibility with the proposed sequence, but does not establish activation causality or GREY eligibility."
        if observed else
        f"No correlatable GREY-to-ACTIVE transition was recorded; {successes} collections were confirmed. This run cannot establish the proposed mechanism or refute it from missing transitions."
    )
    durable_json(root / "final-report.json", report)
    lines = [f"Run: {report['run_id']}", f"Status: {report['status']}",
             f"Started: {report['run_started_at']}", f"Planned end: {report['planned_end_at']}",
             f"Ended: {report['ended_at']}", "Historical collected: UNKNOWN", "",
             "| Account | Confirmed gifts | GREY episodes | GREY→ACTIVE | Attempts | Failures | Restarts | Recoveries | Outside-bound samples | Max consecutive stuck |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for account, value in report["accounts"].items():
        lines.append(f"| {account} | {value['total_successful_gifts']} | {value['grey_detection_episodes']} | {value['grey_to_active_transitions']} | {value['collection_attempts']} | {value['collection_failures']} | {value['worker_restarts']} | {value['recoveries']} | {value['outside_boundary_observations']} | {value['max_consecutive_stuck_movements']} |")
    for account, value in report["accounts"].items():
        lines.extend(["", f"Account {account}:", f"Success timestamps: {value['success_timestamps']}",
                      f"GREY→ACTIVE latency (seconds): {value['grey_to_active_latency_seconds']}",
                      f"Success intervals (seconds): {value['success_intervals_seconds']}",
                      f"Health/crash events: {value['health_events']}"])
    lines.extend(["", report["hypothesis_assessment"], "",
                  "Counts describe THIS run only. State-event samples cannot prove the absence of an unobserved brief drift."])
    (root / "final-report.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.run_dir
    meta = json.loads((root / "metadata.json").read_text())
    if meta["status"] in {"COMPLETED", "BLOCKED"}:
        return
    load_dotenv(".data/linux-validation.env")
    api = API(os.environ["ORCHESTRATOR_PUBLIC_URL"])
    startup_deadline = time.monotonic() + 360
    previous = {}
    last_summary, last_collect = 0., 0.
    for account in GUESTS:
        entry = meta["accounts"][str(account)]
        if (root / f"account-{account}-operator-stopped").exists():
            continue
        w = worker_report(api.request("GET", f"accounts/{account}/worker"))
        session = w.get("telemetry", {}).get("locomotion", {}).get("session_id")
        if session != entry["session_id"]:
            if meta.get("run_started_at"):
                meta["status"] = "BLOCKED"
                entry.setdefault("health_events", []).append({"at": stamp(), "event": "ANCHOR_SESSION_LOST"})
                append_jsonl(root / "alerts.jsonl", {"timestamp": stamp(), "account": account, "event": "ANCHOR_SESSION_LOST"})
                for affected in GUESTS:
                    api.command(affected, "DISABLED")
                    collect_events(root, meta, affected)
                durable_json(root / "metadata.json", meta)
                finish_report(root, meta)
                return
            if w.get("mode") != "DISABLED":
                api.command(account, "DISABLED")
            api.command(account, "ACTIVE", locomotion_profile="CONTROL",
                        experiment_session_id=entry["session_id"], experiment_seconds=18600,
                        experiment_until_gift=False, experiment_continue_after_claim=True)
            entry["activated_at"] = stamp()
            durable_json(root / "metadata.json", meta)
    while True:
        now = time.monotonic()
        all_ready = True
        for account in GUESTS:
            entry = meta["accounts"][str(account)]
            try:
                state = api.request("GET", f"accounts/{account}")
                payload = api.request("GET", f"accounts/{account}/worker")
                w = worker_report(payload)
                t = w.get("telemetry", {})
                movement = t.get("locomotion", {})
                zone = movement.get("station_zone", {})
                frame_age = age(w.get("last_observation_at"))
                action = str(w.get("last_action", ""))
                receipt = t.get("inworld_gift_confirmation") or {}
                retryable_local = (
                    (action.startswith("HOVER_GIFT_ICON") and "TIMED_OUT" in action)
                    or (action.startswith("CLICK_LOCAL_TARGET")
                        and any(status in action for status in ("FAILED", "TIMED_OUT")))
                    or (action.startswith("CLICK_INWORLD_USE_LATER")
                        and receipt.get("active_gift_icon_disappeared") is True
                        and receipt.get("backend", {}).get("http_status") == 200
                        and receipt.get("backend", {}).get("error") is False)
                )
                if (w.get("error_code") == "WORKER_INTERVENTION_REQUIRED"
                        and retryable_local and w.get("healthy")
                        and state.get("worker_phase") == "GAME_READY"
                        and age(entry.get("hover_resume_at")) > 30):
                    command = api.request("POST", f"accounts/{account}/worker/resume", {})
                    entry["hover_resume_at"] = stamp()
                    entry.setdefault("health_events", []).append({"at": stamp(), "event": "SAFE_LOCAL_INTERVENTION_RESUME", "command_id": command["id"], "action": action})

                operator_stopped = (root / f"account-{account}-operator-stopped").exists()
                valid_session = operator_stopped or movement.get("session_id") == entry["session_id"]
                ready = (state.get("state") == "RUNNING" and state.get("worker_phase") == "GAME_READY"
                         and w.get("mode") == "ACTIVE" and w.get("healthy") and valid_session
                         and frame_age < 15 and zone.get("displacement") is not None
                         and abs(zone["displacement"][0]) <= 32 and abs(zone["displacement"][1]) <= 20
                         and (movement.get("moving_seconds", 0) > .025 or movement.get("verified_movements", 0) > 0)
                         and movement.get("movement_commands", 0) >= 2)
                all_ready &= bool(ready)
                gift = t.get("gift_availability_evidence") or {}
                summary = {"timestamp": stamp(), "run_id": meta["run_id"], "account": account,
                           "runtime": payload.get("runtime_id"), "GAME_READY": state.get("worker_phase") == "GAME_READY",
                           "worker_mode": w.get("mode"), "worker_state": w.get("state"), "worker_alive": w.get("healthy"),
                           "session_valid": valid_session, "frame_age": frame_age,
                           "movement": movement, "gift": gift,
                           "run_collected": t.get("gift_sequence_in_run", 0),
                           "gift_state": "ACTIVE" if gift.get("availability") == "GIFT_AVAILABLE" else "GREY" if gift.get("icon_present") else "NONE",
                           "movement_active": w.get("mode") == "ACTIVE" and not zone.get("gift_latched") and not zone.get("anchor_lost"),
                           "gift_claim_state": t.get("gift_claim_state"),
                           "last_success_age": age((t.get("inworld_gift_confirmation") or {}).get("observed_at")) if t.get("inworld_gift_confirmation") else None,
                           "last_success": (t.get("inworld_gift_confirmation") or {}).get("claim_timestamp"),
                           "last_state_transition": (w.get("details", {}).get("transitions") or [])[-1:]}
                signature = (w.get("state"), w.get("mode"), state.get("worker_phase"),
                             w.get("restart_count"), t.get("recoveries"), valid_session,
                             zone.get("anchor_lost"), t.get("gift_sequence_in_run", 0))
                if signature != previous.get(account) or now - last_summary >= 60:
                    if previous.get(account) and (signature[3:5] != previous[account][3:5]
                            or not valid_session or w.get("state") in {"ERROR", "NEEDS_ATTENTION"}
                            or signature[2] != previous[account][2]):
                        entry.setdefault("health_events", []).append({"at": stamp(), "event": "HEALTH_TRANSITION", "signature": signature})
                    append_jsonl(root / "telemetry.jsonl", summary)
                    previous[account] = signature
                entry["live"] = summary
                entry["run_collected"] = t.get("gift_sequence_in_run", 0)
                if not valid_session and meta.get("run_started_at"):
                    entry.setdefault("health_events", []).append({"at": stamp(), "event": "ANCHOR_SESSION_LOST"})
                    meta["status"] = "BLOCKED"
                    api.command(account, "DISABLED")
                if not ready:
                    entry.setdefault("unready_since", stamp())
                else:
                    entry.pop("unready_since", None)
                if not operator_stopped and entry.get("unready_since") and age(entry["unready_since"]) > 120 and meta.get("run_started_at"):
                    append_jsonl(root / "alerts.jsonl", {"timestamp": stamp(), "account": account,
                                 "event": "WORKER_HEALTH_OR_MOVEMENT_ISSUE", "live": summary})
                if now - last_collect >= 60:
                    collect_events(root, meta, account)
            except (httpx.HTTPError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                all_ready = False
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
                    api = API(os.environ["ORCHESTRATOR_PUBLIC_URL"])
                append_jsonl(root / "alerts.jsonl", {"timestamp": stamp(), "account": account,
                                                    "event": "MONITOR_ERROR", "error": str(exc)[:200]})
        if all_ready and not meta.get("run_started_at"):
            start = datetime.now(UTC)
            meta.update(status="RUNNING", run_started_at=start.isoformat(), planned_end_at=(start+timedelta(hours=5)).isoformat())
            print(json.dumps({k: meta[k] for k in ("status", "run_id", "run_started_at", "planned_end_at")}), flush=True)
        if not meta.get("run_started_at") and now > startup_deadline:
            meta["status"] = "BLOCKED"
        ended = meta.get("planned_end_at") and datetime.now(UTC) >= datetime.fromisoformat(meta["planned_end_at"])
        if ended or meta["status"] == "BLOCKED":
            for account in GUESTS:
                api.command(account, "DISABLED")
                collect_events(root, meta, account)
            if ended:
                meta["status"] = "COMPLETED"
            meta["ended_at"] = stamp()
            durable_json(root / "metadata.json", meta)
            finish_report(root, meta)
            return
        durable_json(root / "metadata.json", meta)
        if now - last_summary >= 60:
            last_summary = now
        if now - last_collect >= 60:
            last_collect = now
        time.sleep(5)


if __name__ == "__main__":
    main()
