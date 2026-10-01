#!/usr/bin/env python3
"""Run two isolated canonical workers, including a fixed-profile gift baseline."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_agent.gameworker.locomotion import PROFILES
from runtime_agent.gameworker.reward_evidence import durable_json


class API:
    def __init__(self, base):
        self.base = base.rstrip("/") + "/api/v1/"
        self.client = httpx.Client(timeout=30)
        login = self.request("POST", "auth/login", {
            "username": os.getenv("ADMIN_USERNAME", "admin"),
            "password": os.environ["ADMIN_PASSWORD"],
        })
        self.client.headers["x-csrf-token"] = login["csrf_token"]

    def request(self, method, path, data=None):
        response = self.client.request(method, self.base + path, json=data)
        response.raise_for_status()
        return response.json()

    def command(self, account, mode, **values):
        command = self.request("POST", f"accounts/{account}/worker/mode", {"mode": mode, **values})
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = self.request("GET", f"accounts/{account}/worker/commands/{command['id']}")
            if status["status"] == "COMPLETED":
                if status["result"] != "OK":
                    raise RuntimeError(f"Account {account} command ACK: {status['result']}")
                return
            time.sleep(1)
        raise TimeoutError(f"Account {account} command ACK timeout")

    def start(self, account):
        state = self.request("GET", f"accounts/{account}")
        if state["state"] in {"STOPPED", "READY"}:
            job = self.request("POST", f"accounts/{account}/start", {})["job"]
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                result = self.request("GET", f"jobs/{job['id']}")
                if result["status"] == "SUCCEEDED":
                    break
                if result["status"] in {"FAILED", "CANCELLED"}:
                    raise RuntimeError(f"Account {account} start failed: {result['last_error_code']}")
                time.sleep(2)
            else:
                raise TimeoutError(f"Account {account} start timeout")
        state = self.request("GET", f"accounts/{account}")
        if (state.get("state") != "RUNNING" or state.get("worker_phase") != "GAME_READY"
                or not state.get("verified_at")):
            raise RuntimeError(f"Account {account} requires canonical runtime readiness/verification")
        return state

    def reserve(self, accounts):
        # Persistently pause the existing scheduler before claiming worker input.
        # Do not resume it on exit: that would silently start another long run.
        remaining = set(accounts)
        deadline = time.monotonic() + 900
        while remaining:
            for account in tuple(remaining):
                state = self.request("POST", f"accounts/{account}/schedule/pause", {})
                if not state["active_jobs"]:
                    remaining.remove(account)
            if remaining:
                if time.monotonic() >= deadline:
                    raise TimeoutError("existing account jobs did not drain; no experiment input started")
                time.sleep(2)


def worker_report(status):
    return status.get("details", {}).get("diagnostics", {}).get("worker", {})


def append_jsonl(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        remaining = memoryview(data)
        while remaining:
            remaining = remaining[os.write(fd, remaining):]
        os.fsync(fd)
    finally:
        os.close(fd)


def utc_now():
    return datetime.now(UTC).isoformat()


def _parse_psi(text):
    result = {}
    for line in text.splitlines():
        key, _, rest = line.partition(" ")
        result[key] = {name: float(value) for part in rest.split()
                       for name, _, value in [part.partition("=")]}
    return result


def host_resources():
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        mem[key] = int(value.strip().split()[0]) * 1024
    vm = {}
    for line in Path("/proc/vmstat").read_text().splitlines():
        key, value = line.split()
        if key in {"pswpin", "pswpout"}:
            vm[key] = int(value)
    stat = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    cpu = [int(x) for x in stat]
    return {"ram_total_bytes": mem["MemTotal"], "ram_available_bytes": mem["MemAvailable"],
            "swap_used_bytes": mem["SwapTotal"] - mem["SwapFree"], "swap_io_pages": vm,
            "load": [float(x) for x in Path("/proc/loadavg").read_text().split()[:3]],
            "cpu_ticks": cpu, "cpu_psi": _parse_psi(Path("/proc/pressure/cpu").read_text()),
            "memory_psi": _parse_psi(Path("/proc/pressure/memory").read_text()),
            "disk_free_bytes": shutil.disk_usage(".").free}


def runtime_resources(container):
    script = r'''import json
from pathlib import Path
p=Path('/sys/fs/cgroup')
def read(name):
 try: return (p/name).read_text()
 except OSError: return ''
mem=int(read('memory.current') or 0)
stat={k:int(v) for line in read('memory.stat').splitlines() for k,_,v in [line.partition(' ')] if v.isdigit()}
cpu={k:int(v) for line in read('cpu.stat').splitlines() for k,_,v in [line.partition(' ')] if v.isdigit()}
events={k:int(v) for line in read('memory.events').splitlines() for k,_,v in [line.partition(' ')] if v.isdigit()}
procs={}
for item in Path('/proc').iterdir():
 if item.name.isdigit():
  try:
   name=(item/'comm').read_text().strip()
   if name == 'steam' or name.startswith('dontstarve'): procs.setdefault(name,[]).append(int(item.name))
  except OSError: pass
print(json.dumps({'ram_bytes':mem,'working_set_bytes':max(0,mem-stat.get('inactive_file',0)),
 'cpu_usage_usec':cpu.get('usage_usec',0),'cpu_psi':read('cpu.pressure'),
 'memory_psi':read('memory.pressure'),'memory_events':events,'processes':procs}))'''
    result = subprocess.run(["incus", "exec", container, "--", "python3", "-c", script],
                            capture_output=True, text=True, timeout=12, check=False)
    result.check_returncode()
    return json.loads(result.stdout)


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lo, hi = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def confirmed_claim_timestamp(receipt, account):
    """Accept only a native in-world completion for this account."""
    if not receipt or receipt.get("account_id") != account:
        return None
    backend = receipt.get("backend") or {}
    if (receipt.get("semantic") != "IN_WORLD_GIFT_CONFIRMED"
            or backend.get("operation") != "SetItemOpened_Complete"
            or backend.get("http_status") != 200 or backend.get("error") is not False):
        return None
    confirmed = receipt.get("claim_timestamp") or backend.get("modified")
    if isinstance(confirmed, (int, float)):
        confirmed = datetime.fromtimestamp(confirmed, UTC).isoformat()
    if not isinstance(confirmed, str):
        return None
    parsed = datetime.fromisoformat(confirmed.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return None
    return parsed.isoformat()


def summarize_account(state, target_hours, max_wall_hours):
    base = state.get("measurement_base", {"valid": 0.0, "active": 0.0, "moving": 0.0,
                                          "idle": 0.0, "commands": 0, "direction_changes": 0})
    measured = {k: max(0, state["totals"][k] - base.get(k, 0)) for k in state["totals"]}
    valid_h = measured["valid"] / 3600
    wall_h = max(0.0, (time.time() - state["started_epoch"]) / 3600)
    gifts = state["gifts"]
    intervals = [g["valid_online_world_seconds_since_previous_claim"] / 3600
                 for g in gifts if not g.get("preexisting_at_run_start")
                 and not g.get("interval_left_censored", False)]
    measured_gifts = sum(not g.get("preexisting_at_run_start") for g in gifts)
    result = {"account_id": state["identity"]["account_id"], "runtime_id": state["identity"]["runtime_id"],
              "profile": state["identity"]["profile"], "valid_online_hours": round(valid_h, 4),
              "wall_clock_hours": round(wall_h, 4), "confirmed_gifts": len(gifts),
              "preexisting_gifts": sum(bool(g.get("preexisting_at_run_start")) for g in gifts),
              "clean_measured_intervals": len(intervals), "intervals_valid_hours": [round(x, 4) for x in intervals],
              "interval_min_hours": min(intervals) if intervals else None,
              "interval_median_hours": statistics.median(intervals) if intervals else None,
              "interval_mean_hours": statistics.mean(intervals) if intervals else None,
              "interval_max_hours": max(intervals) if intervals else None,
              "interval_p75_hours": percentile(intervals, .75) if len(intervals) >= 4 else None,
              "interval_p90_hours": percentile(intervals, .90) if len(intervals) >= 4 else None,
              "gifts_per_valid_online_hour": measured_gifts / valid_h if valid_h else None,
              "valid_hours_per_gift": valid_h / measured_gifts if measured_gifts else None,
              "movement_commands_per_hour": measured["commands"] / valid_h if valid_h else None,
              "moving_seconds_per_hour": measured["moving"] / valid_h if valid_h else None,
              "disconnect_seconds": state["disconnect_seconds"] + (
                  time.time() - state["disconnect_since"] if state.get("disconnect_since") else 0),
              "recovery_seconds": state["recovery_seconds"] + (
                  time.time() - state["recovery_since"] if state.get("recovery_since") else 0),
              "runtime_restarts": state["runtime_restarts"], "worker_restarts": state["worker_restarts"],
              "max_heartbeat_age_seconds": state.get("max_heartbeat_age_seconds"),
              "max_heartbeat_gap_seconds": state.get("max_heartbeat_gap_seconds"),
              "weekly_cap_reached": state.get("weekly_cap_reached", False),
              "stop_reason": state.get("stop_reason"), "technical_failure": state.get("technical_failure"),
              "target_valid_hours": target_hours, "max_wall_hours": max_wall_hours}
    return result


def render_summary(summary):
    lines = [f"# Gift characterization {summary['run_id']}", "",
             f"Source commit: `{summary['source_commit']}`",
             f"Configuration SHA-256: `{summary['configuration_sha256']}`", "",
             f"Pooled estimated valid hours / 8 gifts: {summary['pooled_estimated_valid_hours_per_8_gifts']}",
             f"HIGH_ACTIVITY / CONTROL movement intensity ratio: {summary['high_activity_control_movement_intensity_ratio']}", ""]
    for account, item in summary["accounts"].items():
        lines += [f"## Account {account} — {item['profile']}", "",
                  f"- Valid online: {item['valid_online_hours']} h; wall clock: {item['wall_clock_hours']} h",
                  f"- Confirmed gifts: {item['confirmed_gifts']}; preexisting: {item['preexisting_gifts']}; clean intervals: {item['clean_measured_intervals']}",
                  f"- Intervals (valid h): {item['intervals_valid_hours']}",
                  f"- Median: {item['interval_median_hours']}; range: {item['interval_min_hours']}–{item['interval_max_hours']}",
                  f"- Mean: {item['interval_mean_hours']}; p75: {item['interval_p75_hours']}; p90: {item['interval_p90_hours']}",
                  f"- Gifts / valid h: {item['gifts_per_valid_online_hour']}; projected valid h / 8 gifts: {summary.get('profile_estimated_valid_hours_per_8_gifts', {}).get(account)}",
                  f"- Movement: {item['movement_commands_per_hour']} commands/h, {item['moving_seconds_per_hour']} moving s/h",
                  f"- Disconnect: {item['disconnect_seconds']} s; recovery: {item['recovery_seconds']} s; restarts: runtime {item['runtime_restarts']}, worker {item['worker_restarts']}",
                  f"- Weekly cap reached: {item['weekly_cap_reached']}; stop: {item['stop_reason']}; technical failure: {item['technical_failure']}", ""]
    lines += [f"Pooled estimated valid hours / 8 gifts: {summary['pooled_estimated_valid_hours_per_8_gifts']}",
              "This is the observed reward timing projection; it does not change production strategy.", ""]
    return "\n".join(lines)


def status_characterization(run_id, output):
    root = output / run_id
    metadata = json.loads((root / "metadata.json").read_text())
    unit = f"dst-gift-characterization-{run_id}.service"
    service = subprocess.run(["sudo", "-n", "systemctl", "is-active", unit],
                              capture_output=True, text=True, check=False).stdout.strip()
    latest = {}
    for path in root.glob("account-*/**/telemetry.jsonl"):
        lines = path.read_text().splitlines()
        if lines:
            row = json.loads(lines[-1])
            latest[str(row["account_id"])] = {"profile": row["profile"], "mode": row["mode"],
                                              "state": row["state"], "screen": row["screen"],
                                              "sampled_at": row["sampled_at"], "telemetry": row["telemetry"]}
    print(json.dumps({"service": unit, "service_state": service, "metadata": metadata,
                      "latest_accounts": latest, "run_root": str(root)}, indent=2, sort_keys=True))
    return 0


def run_characterization(args, api):
    target = args.target_valid_hours * 3600
    wall_limit = args.max_wall_hours * 3600
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    root = (args.output or Path(".data/gift-characterization")) / run_id
    root.mkdir(parents=True, exist_ok=False)
    (root / "screenshots").mkdir()
    profiles = dict(zip(args.accounts, ("CONTROL", "HIGH_ACTIVITY"), strict=True))
    known_t0 = dict.fromkeys(args.accounts)
    states, streams = {}, {}
    event_path, gifts_path, samples_path = root / "events.jsonl", root / "gifts.jsonl", root / "samples.jsonl"
    started = time.time()
    source_commit = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    working_tree = subprocess.run(["git", "status", "--porcelain"], check=True,
                                  capture_output=True, text=True).stdout.strip()
    if working_tree:
        raise RuntimeError("experiment version must start from a clean committed tree")
    config = {"run_id": run_id, "target_valid_online_hours_per_account": args.target_valid_hours,
              "max_wall_clock_hours": args.max_wall_hours, "continue_after_claim": True,
              "profiles": {name: list(PROFILES[name]) for name in set(profiles.values())},
              "source_commit": source_commit, "accounts": args.accounts,
              "sample_interval_seconds": 15, "resource_interval_seconds": 300,
              "preclaim_max_seconds": args.preclaim_max_seconds,
              "resource_stop_thresholds": {"new_runtime_oom_kills": 1, "swap_io_bytes_per_5m": 536870912}}
    config_hash = None
    metadata = {**config, "started_at": utc_now(), "run_root": str(root), "status": "STARTING",
                "baseline_t0": {str(k): known_t0[k] for k in args.accounts}}
    durable_json(root / "metadata.json", metadata)
    try:
        api.reserve(args.accounts)
        for account in args.accounts:
            runtime = api.start(account)
            deployment = subprocess.run(["incus", "exec", runtime["external_id"], "--", "cat",
                                         "/opt/dst-orchestrator/DEPLOYMENT.json"],
                                        capture_output=True, text=True, timeout=10, check=False)
            try:
                runtime_version = json.loads(deployment.stdout)
            except (ValueError, TypeError):
                runtime_version = {"revision": None}
            identity = {"account_id": account, "runtime_id": runtime["runtime_id"],
                        "runtime_generation": runtime.get("runtime_generation", runtime.get("generation")),
                        "container": runtime["external_id"], "profile": profiles[account],
                        "run_id": run_id,
                        "session_id": f"gift-{run_id}-a{account}-r{runtime['runtime_id']}",
                        "guest_evidence": f"/home/dst/.local/state/dst-runtime/experiments/gift-{run_id}-a{account}-r{runtime['runtime_id']}",
                        "runtime_version": runtime_version}
            prior_report = worker_report(api.request("GET", f"accounts/{account}/worker"))
            prior_receipt = (prior_report.get("telemetry") or {}).get("inworld_gift_confirmation")
            known_t0[account] = confirmed_claim_timestamp(prior_receipt, account)
            state = {"identity": identity, "started_epoch": started, "previous_claim_at": known_t0[account],
                     "last_claim_epoch": (datetime.fromisoformat(known_t0[account]).timestamp()
                                          if known_t0[account] else None),
                     "interval_left_censored": True,
                     "preexisting_pending": False, "gifts": [],
                     "totals": {"valid": 0.0, "active": 0.0, "moving": 0.0, "idle": 0.0,
                                "commands": 0, "direction_changes": 0},
                     "last_worker_totals": None, "last_sample": None, "fail_samples": 0,
                     "disconnect_since": None, "recovery_since": None,
                     "disconnect_seconds": 0.0, "recovery_seconds": 0.0,
                     "runtime_restarts": 0, "worker_restarts": 0,
                     "last_runtime_pids": None, "last_worker_restart_count": None,
                     "max_heartbeat_age_seconds": 0.0,
                     "max_heartbeat_gap_seconds": 0.0, "last_heartbeat_epoch": None,
                     "weekly_cap_reached": False, "technical_failure": None, "stop_reason": None,
                     "last_observation_frame": None,
                     "last_gift_item_id": prior_receipt.get("item_id") if known_t0[account] else None,
                     "measurement_base": {"valid": 0.0, "active": 0.0, "moving": 0.0,
                                           "idle": 0.0, "commands": 0, "direction_changes": 0}}
            states[account] = state
            folder = root / f"account-{account}" / identity["session_id"]
            folder.mkdir(parents=True)
            if known_t0[account]:
                baseline_receipt = folder / "baseline-receipt.json"
                durable_json(baseline_receipt, prior_receipt)
                state["baseline_receipt_path"] = str(baseline_receipt)
            streams[account] = (folder / "telemetry.jsonl").open("a", buffering=1)
            # Refresh both gift HUD and native inventory evidence without gameplay input.
            api.command(account, "OBSERVE")
        baseline_deadline = time.monotonic() + 45
        baseline_done = set()
        while len(baseline_done) != len(args.accounts) and time.monotonic() < baseline_deadline:
            for account in args.accounts:
                if account in baseline_done:
                    continue
                state = states[account]
                report = worker_report(api.request("GET", f"accounts/{account}/worker"))
                telemetry = report.get("telemetry", {})
                observation = report.get("details", {}).get("observation", {})
                gift = telemetry.get("gift_availability_evidence") or {}
                observed = gift.get("observed_at")
                if (observed and observed != state.get("baseline_observed_at")
                        and observation.get("screen") == "IN_WORLD_IDLE"
                        and observation.get("validity") == "VALID"):
                    state["baseline_observed_at"] = observed
                    state["preexisting_pending"] = (
                        gift.get("availability") in {"GIFT_AVAILABLE", "IN_WORLD_GIFT_PENDING"}
                        and gift.get("icon_present") is True
                        and gift.get("identity_confidence", 0) >= .94
                    )
                    state["baseline_gift"] = gift
                    state["baseline_item_service"] = telemetry.get("item_service")
                    state["baseline_worker_version"] = report.get("version")
                    baseline_done.add(account)
            time.sleep(2)
        for account in args.accounts:
            state = states[account]
            try:
                if account not in baseline_done:
                    raise TimeoutError(f"Account {account} did not produce fresh baseline gift evidence")
                api.command(account, "DISABLED")
                gift = state["baseline_gift"]
                claimable = state["preexisting_pending"]
                if claimable:
                    pre_session = f"gift-{run_id}-a{account}-r{state['identity']['runtime_id']}-preexisting"
                    append_jsonl(event_path, {"event": "PREEXISTING_AT_RUN_START", "at": utc_now(),
                                              **state["identity"], "visual_state": gift,
                                              "item_service_state": state.get("baseline_item_service"),
                                              "preclaim_session_id": pre_session})
                    api.command(account, "ACTIVE", locomotion_profile=profiles[account],
                                experiment_session_id=pre_session, experiment_seconds=args.preclaim_max_seconds,
                                experiment_until_gift=True, experiment_continue_after_claim=False)
                    pre_deadline = time.monotonic() + args.preclaim_max_seconds
                    pre_receipt = None
                    while time.monotonic() < pre_deadline:
                        report = worker_report(api.request("GET", f"accounts/{account}/worker"))
                        pre_receipt = report.get("telemetry", {}).get("inworld_gift_confirmation")
                        if pre_receipt:
                            break
                        if (report.get("state") in {"ERROR", "NEEDS_ATTENTION"}
                                or report.get("error_code") == "WORKER_INTERVENTION_REQUIRED"):
                            raise RuntimeError(f"preexisting gift claim failed for account {account}: {report.get('error_code')}")
                        time.sleep(2)
                    if not pre_receipt:
                        raise TimeoutError(f"preexisting gift remained claimable but was not claimed for account {account}")
                    api.command(account, "DISABLED")
                    pre_path = root / f"account-{account}" / state["identity"]["session_id"] / "preexisting-receipt.json"
                    pre_guest = f"{state['identity']['container']}/home/dst/.local/state/dst-runtime/experiments/{pre_session}/gifts/gift-0001/claim.json"
                    pulled = subprocess.run(["incus", "file", "pull", pre_guest, str(pre_path)],
                                            capture_output=True, timeout=15, check=False)
                    if pulled.returncode:
                        durable_json(pre_path, pre_receipt)
                    durable_receipt = json.loads(pre_path.read_text())
                    pre_screenshots = []
                    for kind in ("detection", "completion"):
                        guest_png = (f"{state['identity']['container']}/home/dst/.local/state/dst-runtime/experiments/"
                                     f"{pre_session}/gifts/gift-0001/{kind}.png")
                        local_png = root / "screenshots" / f"account-{account}-preexisting-{kind}.png"
                        pulled_png = subprocess.run(["incus", "file", "pull", guest_png, str(local_png)],
                                                    capture_output=True, timeout=15, check=False)
                        if not pulled_png.returncode:
                            pre_screenshots.append(str(local_png))
                    confirmed = confirmed_claim_timestamp(durable_receipt, account)
                    if not confirmed:
                        raise RuntimeError("preexisting gift had no confirmed native claim timestamp")
                    state["previous_claim_at"] = confirmed
                    state["last_claim_epoch"] = datetime.fromisoformat(confirmed.replace("Z", "+00:00")).timestamp()
                    state["preexisting_receipt"] = durable_receipt
                    state["preexisting_receipt_path"] = str(pre_path)
                    state["last_gift_item_id"] = durable_receipt.get("item_id")
                    pre_gift = {"run_id": run_id, **state["identity"], "gift_sequence_in_run": 0,
                                "weekly_gift_index": None, "weekly_gift_count": None,
                                "previous_claim_confirmed_at": known_t0[account],
                                "gift_first_detected_at": state["baseline_observed_at"],
                                "claim_started_at": None, "claim_confirmed_at": confirmed,
                                "wall_clock_since_previous_claim": max(0, time.time() - state["started_epoch"]),
                                "valid_online_world_seconds_since_previous_claim": 0,
                                "active_elapsed_since_previous_claim": 0, "moving_seconds_since_previous_claim": 0,
                                "idle_seconds_since_previous_claim": 0, "movement_commands": 0,
                                "direction_changes": 0, "disconnect_seconds": 0, "recovery_seconds": 0,
                                "death_respawn_events": 0, "game_lost_events": 0, "worker_pauses": 0,
                                "runtime_restarts": 0, "gift_visual_state": gift,
                                "item_service_state": state.get("baseline_item_service"),
                                "receipt_path": str(pre_path), "evidence_paths": [str(pre_path)],
                                "screenshot_paths": pre_screenshots, "preexisting_at_run_start": True,
                                "item_id": durable_receipt.get("item_id"), "durable_receipt": durable_receipt}
                    append_jsonl(gifts_path, pre_gift)
                    state["gifts"].append(pre_gift)
                    append_jsonl(event_path, {"event": "CLAIM_CONFIRMED", "at": confirmed,
                                              **state["identity"], "preexisting_at_run_start": True,
                                              "item_id": durable_receipt.get("item_id"), "receipt_path": str(pre_path)})
                    append_jsonl(event_path, {"event": "RECEIPT_SAVED", "at": utc_now(),
                                              **state["identity"], "receipt_path": str(pre_path)})
                state["started_epoch"] = time.time()
                state["measurement_base"] = dict(state["totals"])
                state["measurement_base_valid_set"] = True
                append_jsonl(event_path, {"event": "BASELINE_GIFT_STATE", "at": utc_now(), **state["identity"],
                                          "preexisting_at_run_start": state["preexisting_pending"],
                                          "gift_visual_state": state["baseline_gift"],
                                          "item_service_state": state.get("baseline_item_service"),
                                          "previous_claim_confirmed_at": state["previous_claim_at"]})
            except (httpx.HTTPError, RuntimeError, TimeoutError, ValueError, OSError) as exc:
                state["technical_failure"] = f"baseline preparation: {type(exc).__name__}: {str(exc)[:200]}"
                state["stop_reason"] = "TECHNICAL_FAILURE"
                append_jsonl(event_path, {"event": "TECHNICAL_FAILURE", "at": utc_now(),
                                          **state["identity"], "phase": "BASELINE_PREPARATION",
                                          "reason": state["technical_failure"]})
                try:
                    api.command(account, "DISABLED")
                except (httpx.HTTPError, RuntimeError, TimeoutError):
                    pass
        if not any(state["stop_reason"] is None for state in states.values()):
            raise RuntimeError("no account completed authoritative baseline preparation")
        config["runtimes"] = {str(a): states[a]["identity"]["runtime_version"] for a in args.accounts}
        config["worker_versions"] = {str(a): states[a].get("baseline_worker_version") for a in args.accounts}
        config["baseline_gift_states"] = {str(a): {
            "availability": states[a].get("baseline_gift", {}).get("availability"),
            "item_service_state": (states[a].get("baseline_item_service") or {}).get("state"),
            "pending_items": (states[a].get("baseline_item_service") or {}).get("pending_items"),
            "preexisting_at_run_start": states[a]["preexisting_pending"],
            "previous_claim_confirmed_at": states[a]["previous_claim_at"],
            "baseline_receipt_path": states[a].get("baseline_receipt_path"),
            "technical_failure": states[a]["technical_failure"]} for a in args.accounts}
        config_hash = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        metadata.update(config, baseline_t0={str(a): states[a]["previous_claim_at"] for a in args.accounts},
                        configuration_sha256=config_hash, baseline_observed_at={
            str(a): states[a].get("baseline_observed_at") for a in args.accounts},
            accounts={str(a): states[a]["identity"] for a in args.accounts}, status="BASELINE_READY")
        durable_json(root / "metadata.json", metadata)
        # Arm both workers only after the baseline snapshot is durable.
        experiment_started = utc_now()
        append_jsonl(event_path, {"event": "RUN_STARTED", "at": experiment_started, "run_id": run_id,
                                  "source_commit": source_commit, "configuration_sha256": config_hash})
        for account in args.accounts:
            state = states[account]
            if state["technical_failure"]:
                continue
            api.command(account, "ACTIVE", locomotion_profile=profiles[account],
                        experiment_session_id=state["identity"]["session_id"],
                        experiment_seconds=wall_limit, experiment_until_gift=False,
                        experiment_target_valid_seconds=target,
                        experiment_continue_after_claim=True)
            append_jsonl(event_path, {"event": "GAME_READY", "at": utc_now(), **state["identity"]})
            start_png = root / "screenshots" / f"account-{account}-run-start.png"
            guest_png = f"{state['identity']['container']}{state['identity']['guest_evidence']}/run-start.png"
            pulled = subprocess.run(["incus", "file", "pull", guest_png, str(start_png)],
                                    capture_output=True, timeout=15, check=False)
            if pulled.returncode:
                append_jsonl(event_path, {"event": "SCREENSHOT_CAPTURE_UNAVAILABLE", "at": utc_now(),
                                          **state["identity"], "path": "run-start.png"})
        metadata.update(status="RUNNING", actual_started_at=experiment_started)
        durable_json(root / "metadata.json", metadata)
        wall_deadline = time.monotonic() + wall_limit
        last_resource = 0.0
        last_host = None
        last_runtime_resource = {}
        while time.monotonic() < wall_deadline and any(s["stop_reason"] is None and not s["technical_failure"] for s in states.values()):
            sampled_at = utc_now()
            host = host_resources()
            if last_resource == 0 or time.monotonic() - last_resource >= 300:
                runtimes = {}
                for resource_account, resource_state in states.items():
                    if not resource_state.get("stop_reason") and not resource_state.get("technical_failure"):
                        try:
                            current_runtime = runtime_resources(resource_state["identity"]["container"])
                            previous_runtime = last_runtime_resource.get(resource_account)
                            if previous_runtime:
                                current_runtime["cpu_cores"] = max(0.0, (current_runtime["cpu_usage_usec"] -
                                    previous_runtime["cpu_usage_usec"]) / 1_000_000 / max(1.0, time.monotonic() - last_resource))
                            for key in (("oom", "oom_kill", "oom_group_kill") if previous_runtime else ()):
                                before = (previous_runtime or {}).get("memory_events", {}).get(key, 0)
                                after = current_runtime.get("memory_events", {}).get(key, 0)
                                if after > before:
                                    resource_state["technical_failure"] = f"runtime OOM event {key} increased"
                                    resource_state["stop_reason"] = "TECHNICAL_FAILURE"
                                    append_jsonl(event_path, {"event": "TECHNICAL_FAILURE", "at": sampled_at,
                                                              **resource_state["identity"], "reason": resource_state["technical_failure"]})
                                    try:
                                        api.command(resource_account, "DISABLED")
                                    except (httpx.HTTPError, RuntimeError, TimeoutError):
                                        pass
                            last_runtime_resource[resource_account] = current_runtime
                            current_pids = current_runtime.get("processes", {})
                            previous_pids = resource_state.get("last_runtime_pids")
                            if previous_pids is not None:
                                for process_name in {**previous_pids, **current_pids}:
                                    if previous_pids.get(process_name) != current_pids.get(process_name):
                                        resource_state["runtime_restarts"] += 1
                                        append_jsonl(event_path, {"event": "RUNTIME_PROCESS_RESTART", "at": utc_now(),
                                                                  **resource_state["identity"], "process": process_name,
                                                                  "previous_pids": previous_pids.get(process_name),
                                                                  "current_pids": current_pids.get(process_name)})
                            resource_state["last_runtime_pids"] = current_pids
                            runtimes[str(resource_account)] = current_runtime
                        except (subprocess.SubprocessError, OSError, ValueError) as exc:
                            runtimes[str(resource_account)] = {"error": type(exc).__name__}
                if last_host:
                    old, new = last_host["cpu_ticks"], host["cpu_ticks"]
                    total_delta = sum(new) - sum(old)
                    idle_delta = (new[3] - old[3]) + (new[4] - old[4] if len(new) > 4 else 0)
                    host["cpu_percent"] = round(100 * (1 - idle_delta / total_delta), 2) if total_delta else None
                    page_size = os.sysconf("SC_PAGE_SIZE")
                    swap_io_bytes = sum(max(0, host["swap_io_pages"][key] - last_host["swap_io_pages"].get(key, 0))
                                        for key in ("pswpin", "pswpout")) * page_size
                    host["swap_io_delta_bytes"] = swap_io_bytes
                    mem_full = host["memory_psi"].get("full", {}).get("avg10", 0)
                    if swap_io_bytes >= 536870912 or (swap_io_bytes >= 67108864 and
                                                       host["ram_available_bytes"] < 1024**3 and mem_full >= 5):
                        for resource_state in states.values():
                            if not resource_state.get("stop_reason"):
                                resource_state["technical_failure"] = "host swap thrashing / memory stall threshold reached"
                                resource_state["stop_reason"] = "TECHNICAL_FAILURE"
                                try:
                                    api.command(resource_state["identity"]["account_id"], "DISABLED")
                                except (httpx.HTTPError, RuntimeError, TimeoutError):
                                    pass
                        append_jsonl(event_path, {"event": "RESOURCE_PRESSURE", "at": sampled_at,
                                                  "swap_io_delta_bytes": swap_io_bytes, "host": host})
                append_jsonl(samples_path, {"sampled_at": sampled_at, "type": "resources", "host": host,
                                            "runtimes": runtimes})
                last_host = host
                last_resource = time.monotonic()
            for account, state in states.items():
                if state["stop_reason"] or state["technical_failure"]:
                    continue
                try:
                    response = api.request("GET", f"accounts/{account}/worker")
                    report = worker_report(response)
                    restart_count = int(report.get("restart_count", 0))
                    if state["last_worker_restart_count"] is not None and restart_count > state["last_worker_restart_count"]:
                        state["worker_restarts"] += restart_count - state["last_worker_restart_count"]
                        append_jsonl(event_path, {"event": "WORKER_RESTART", "at": sampled_at,
                                                  **state["identity"], "restart_count": restart_count})
                    state["last_worker_restart_count"] = restart_count
                    telemetry = report.get("telemetry", {})
                    movement = telemetry.get("locomotion", {})
                    if movement.get("session_id") != state["identity"]["session_id"]:
                        raise RuntimeError("experiment session identity lost")
                    current = {"valid": float(movement.get("valid_online_world_elapsed", 0)),
                               "active": float(movement.get("active_elapsed", 0)),
                               "moving": float(movement.get("moving_seconds", 0)),
                               "idle": float(movement.get("idle_seconds", 0)),
                               "commands": int(movement.get("movement_commands", 0)),
                               "direction_changes": int(movement.get("direction_changes", 0))}
                    prev = state["last_worker_totals"]
                    if prev is None:
                        delta = current
                    else:
                        delta = {k: (current[k] - prev[k] if current[k] >= prev[k] else current[k]) for k in current}
                    for key, value in delta.items():
                        state["totals"][key] += value
                    state["last_worker_totals"] = current
                    observation = report.get("details", {}).get("observation", {})
                    screen = observation.get("screen")
                    last_tick = report.get("last_tick_at")
                    try:
                        heartbeat_age = max(0.0, (datetime.now(UTC) - datetime.fromisoformat(last_tick.replace("Z", "+00:00"))).total_seconds())
                    except (AttributeError, ValueError):
                        heartbeat_age = wall_limit
                    state["max_heartbeat_age_seconds"] = max(state["max_heartbeat_age_seconds"], heartbeat_age)
                    heartbeat_epoch = time.time() - heartbeat_age
                    if state["last_heartbeat_epoch"] is not None:
                        gap = max(0.0, heartbeat_epoch - state["last_heartbeat_epoch"])
                        state["max_heartbeat_gap_seconds"] = max(state["max_heartbeat_gap_seconds"], gap)
                        if gap > 30:
                            append_jsonl(event_path, {"event": "HEARTBEAT_GAP", "at": sampled_at,
                                                      **state["identity"], "gap_seconds": gap})
                    state["last_heartbeat_epoch"] = heartbeat_epoch
                    world = (screen == "IN_WORLD_IDLE" and report.get("mode") == "ACTIVE"
                             and heartbeat_age <= 20.0)
                    if state["recovery_since"] is not None and world:
                        recovered = time.time() - state["recovery_since"]
                        state["recovery_seconds"] += recovered
                        append_jsonl(event_path, {"event": "RECOVERED", "at": sampled_at, **state["identity"], "duration_seconds": recovered})
                        if state.get("last_screen") == "DEAD":
                            append_jsonl(event_path, {"event": "RESPAWN", "at": sampled_at, **state["identity"],
                                                      "recovery_seconds": recovered})
                        state["recovery_since"] = None
                        if state["disconnect_since"] is not None:
                            duration = time.time() - state["disconnect_since"]
                            state["disconnect_seconds"] += duration
                            append_jsonl(event_path, {"event": "RECONNECTED", "at": sampled_at, **state["identity"], "duration_seconds": duration})
                            state["disconnect_since"] = None
                    if not world and state["recovery_since"] is None:
                        state["recovery_since"] = time.time()
                        state["game_lost_events"] = state.get("game_lost_events", 0) + 1
                        if screen == "DEAD":
                            state["death_events"] = state.get("death_events", 0) + 1
                        append_jsonl(event_path, {"event": "GAME_LOST", "at": sampled_at, **state["identity"], "screen": screen, "worker_state": report.get("state")})
                        if screen == "DISCONNECTED":
                            state["disconnect_since"] = time.time()
                            append_jsonl(event_path, {"event": "DISCONNECTED", "at": sampled_at, **state["identity"], "screen": screen})
                        if screen == "DEAD":
                            append_jsonl(event_path, {"event": "DEATH", "at": sampled_at, **state["identity"], "screen": screen})
                    evidence = telemetry.get("gift_availability_evidence") or {}
                    state["last_screen"] = screen
                    frame_id = evidence.get("evidence_frame_id")
                    gift_seq = int(telemetry.get("gift_sequence_in_run", 0)) + 1
                    if (frame_id and evidence.get("availability") in {"GIFT_AVAILABLE", "IN_WORLD_GIFT_PENDING"}
                            and evidence.get("icon_present") is True
                            and evidence.get("identity_confidence", 0) >= .94
                            and state.get("last_gift_detection_sequence") != gift_seq):
                        screenshot_paths = []
                        guest_cycle = f"{state['identity']['container']}{state['identity']['guest_evidence']}/gifts/gift-{gift_seq:04d}/detection.png"
                        local_cycle = root / "screenshots" / f"account-{account}-gift-{gift_seq:04d}-detection.png"
                        pulled = subprocess.run(["incus", "file", "pull", guest_cycle, str(local_cycle)],
                                                capture_output=True, timeout=15, check=False)
                        if not pulled.returncode:
                            screenshot_paths.append(str(local_cycle))
                        append_jsonl(event_path, {"event": "GIFT_DETECTED", "at": evidence.get("observed_at", sampled_at), **state["identity"],
                                                  "gift_sequence_in_run": gift_seq, "gift_visual_state": evidence,
                                                  "item_service_state": telemetry.get("item_service"),
                                                  "screenshot_paths": screenshot_paths})
                        state["last_observation_frame"] = frame_id
                        state["last_gift_detection_sequence"] = gift_seq
                    if telemetry.get("inworld_gift_state") in {"IN_WORLD_GIFT_OPENING", "IN_WORLD_GIFT_RECEIVED"} and state.get("claim_started") is None:
                        state["claim_started"] = sampled_at
                        append_jsonl(event_path, {"event": "CLAIM_STARTED", "at": sampled_at, **state["identity"],
                                                  "gift_sequence_in_run": telemetry.get("gift_sequence_in_run", 0) + 1})
                    receipt = telemetry.get("inworld_gift_confirmation")
                    if receipt and receipt.get("item_id") != state["last_gift_item_id"]:
                        seq = int(receipt.get("gift_sequence_in_run", len(state["gifts"]) + 1))
                        cycle_screens = []
                        local_receipt = root / f"account-{account}" / state["identity"]["session_id"] / f"claim-{seq:04d}.json"
                        guest_receipt = f"{state['identity']['container']}{state['identity']['guest_evidence']}/gifts/gift-{seq:04d}/claim.json"
                        pulled = subprocess.run(["incus", "file", "pull", guest_receipt, str(local_receipt)],
                                                capture_output=True, timeout=15, check=False)
                        if pulled.returncode:
                            raise RuntimeError(f"durable claim receipt pull failed for account {account} gift {seq}")
                        for kind in ("detection", "completion"):
                            guest_file = f"{state['identity']['container']}{state['identity']['guest_evidence']}/gifts/gift-{seq:04d}/{kind}.png"
                            local_file = root / "screenshots" / f"account-{account}-gift-{seq:04d}-{kind}.png"
                            if local_file.exists():
                                continue
                            pulled = subprocess.run(["incus", "file", "pull", guest_file, str(local_file)],
                                                    capture_output=True, timeout=15, check=False)
                            if not pulled.returncode:
                                cycle_screens.append(str(local_file))
                        preexisting = bool(state["preexisting_pending"] and not state["gifts"])
                        confirmed_at = confirmed_claim_timestamp(receipt, account)
                        if (not confirmed_at or receipt.get("runtime_id") != state["identity"]["runtime_id"]
                                or receipt.get("experiment_session_id") != state["identity"]["session_id"]):
                            raise RuntimeError("native claim confirmation identity mismatch")
                        gift_first = (receipt.get("detection_timestamp") or state.get("baseline_observed_at"))
                        valid_since = state["totals"]["valid"]
                        weekly_count = receipt.get("weekly_gift_count") or (telemetry.get("item_service") or {}).get("weekly_gift_count")
                        weekly_index = receipt.get("weekly_gift_index") or (telemetry.get("item_service") or {}).get("weekly_gift_index")
                        interval = {"run_id": run_id, **state["identity"], "gift_sequence_in_run": seq,
                                    "weekly_gift_index": weekly_index, "weekly_gift_count": weekly_count,
                                    "previous_claim_confirmed_at": state["previous_claim_at"],
                                    "gift_first_detected_at": gift_first,
                                    "claim_started_at": state.pop("claim_started", None),
                                    "claim_confirmed_at": confirmed_at,
                                    "wall_clock_since_previous_claim": (max(0.0, datetime.fromisoformat(confirmed_at).timestamp()
                                        - state["last_claim_epoch"]) if state["last_claim_epoch"] else None),
                                    "interval_left_censored": state["interval_left_censored"],
                                    "valid_online_world_seconds_since_previous_claim": valid_since - state.get("last_claim_valid", 0.0),
                                    "active_elapsed_since_previous_claim": state["totals"]["active"] - state.get("last_claim_active", 0.0),
                                    "moving_seconds_since_previous_claim": state["totals"]["moving"] - state.get("last_claim_moving", 0.0),
                                    "idle_seconds_since_previous_claim": state["totals"]["idle"] - state.get("last_claim_idle", 0.0),
                                    "movement_commands": state["totals"]["commands"] - state.get("last_claim_commands", 0),
                                    "direction_changes": state["totals"]["direction_changes"] - state.get("last_claim_directions", 0),
                                    **{out: state.get(key, 0) - state.get("last_claim_events", {}).get(key, 0)
                                       for out, key in (("disconnect_seconds", "disconnect_seconds"),
                                                        ("recovery_seconds", "recovery_seconds"),
                                                        ("death_respawn_events", "death_events"),
                                                        ("game_lost_events", "game_lost_events"),
                                                        ("worker_pauses", "worker_pauses"),
                                                        ("runtime_restarts", "runtime_restarts"))},
                                    "gift_visual_state": telemetry.get("gift_availability_evidence"),
                                    "item_service_state": telemetry.get("item_service"),
                                    "receipt_path": str(local_receipt),
                                    "guest_receipt_path": guest_receipt,
                                    "evidence_paths": [str(local_receipt), guest_receipt],
                                    "screenshot_paths": cycle_screens, "preexisting_at_run_start": preexisting,
                                    "item_id": receipt.get("item_id"), "durable_receipt": receipt}
                        append_jsonl(gifts_path, interval)
                        append_jsonl(event_path, {"event": "CLAIM_CONFIRMED", "at": confirmed_at, **state["identity"], "gift_sequence_in_run": seq,
                                                  "item_id": receipt.get("item_id"), "receipt_path": receipt.get("receipt_path")})
                        append_jsonl(event_path, {"event": "RECEIPT_SAVED", "at": sampled_at, **state["identity"], "gift_sequence_in_run": seq,
                                                  "receipt_path": receipt.get("receipt_path")})
                        state["gifts"].append(interval)
                        state["last_gift_item_id"] = receipt.get("item_id")
                        state["previous_claim_at"] = confirmed_at
                        state["last_claim_epoch"] = datetime.fromisoformat(confirmed_at).timestamp()
                        state["interval_left_censored"] = False
                        state["last_claim_valid"] = valid_since
                        state["last_claim_active"] = state["totals"]["active"]
                        state["last_claim_moving"] = state["totals"]["moving"]
                        state["last_claim_idle"] = state["totals"]["idle"]
                        state["last_claim_commands"] = state["totals"]["commands"]
                        state["last_claim_directions"] = state["totals"]["direction_changes"]
                        state["last_claim_events"] = {key: state.get(key, 0) for key in
                            ("disconnect_seconds", "recovery_seconds", "death_events",
                             "game_lost_events", "worker_pauses", "runtime_restarts")}
                        weekly_cap = receipt.get("weekly_gift_cap") or (telemetry.get("item_service") or {}).get("weekly_gift_cap")
                        if (receipt.get("weekly_cap_reached") is True or
                                isinstance(weekly_count, int) and isinstance(weekly_cap, int) and weekly_count >= weekly_cap):
                            state["weekly_cap_reached"] = True
                            state["stop_reason"] = "WEEKLY_CAP_REACHED"
                            append_jsonl(event_path, {"event": "WEEKLY_CAP_REACHED", "at": sampled_at,
                                                      **state["identity"], "weekly_gift_count": weekly_count,
                                                      "weekly_gift_cap": weekly_cap})
                    record = {"sampled_at": sampled_at, **state["identity"], "mode": report.get("mode"),
                              "state": report.get("state"), "screen": screen, "error": report.get("error_code"),
                              "telemetry": telemetry}
                    streams[account].write(json.dumps(record, sort_keys=True) + "\n")
                    append_jsonl(samples_path, record)
                    state["last_sample"] = record
                    if state.get("last_world") is False and world:
                        append_jsonl(event_path, {"event": "GAME_READY", "at": sampled_at, **state["identity"],
                                                  "recovered": True})
                    state["last_world"] = world
                    prior_mode = state.get("last_mode")
                    paused = report.get("mode") == "PAUSED" or report.get("state") == "PAUSED"
                    previously_paused = prior_mode == "PAUSED" or state.get("last_state") == "PAUSED"
                    if paused and not previously_paused:
                        state["worker_pauses"] = state.get("worker_pauses", 0) + 1
                        append_jsonl(event_path, {"event": "WORKER_PAUSED", "at": sampled_at, **state["identity"]})
                    elif previously_paused and report.get("mode") == "ACTIVE" and report.get("state") != "PAUSED":
                        append_jsonl(event_path, {"event": "WORKER_RESUMED", "at": sampled_at, **state["identity"]})
                    state["last_mode"] = report.get("mode")
                    state["last_state"] = report.get("state")
                    if report.get("state") in {"ERROR", "NEEDS_ATTENTION"}:
                        state["fail_samples"] += 1
                    else:
                        state["fail_samples"] = 0
                    measurement_base = state["measurement_base"]["valid"]
                    if (not state["preexisting_pending"] or state.get("measurement_base_valid_set")) and \
                            state["totals"]["valid"] - measurement_base >= target:
                        state["stop_reason"] = "TARGET_VALID_ONLINE_REACHED"
                    elif report.get("mode") == "DISABLED" and report.get("error_code") in {"TARGET_VALID_ONLINE_REACHED", "MAX_WALL_CLOCK_REACHED"}:
                        state["stop_reason"] = report["error_code"]
                    elif report.get("state") in {"ERROR", "NEEDS_ATTENTION"} and state["fail_samples"] >= 10:
                        state["technical_failure"] = report.get("error_code") or report.get("state")
                        state["stop_reason"] = "TECHNICAL_FAILURE"
                        api.command(account, "DISABLED")
                        append_jsonl(event_path, {"event": "TECHNICAL_FAILURE", "at": sampled_at, **state["identity"], "reason": state["technical_failure"]})
                    elif state["recovery_since"] is not None and time.time() - state["recovery_since"] >= 600:
                        state["technical_failure"] = "existing GameWorker recovery exceeded 10 minutes"
                        state["stop_reason"] = "TECHNICAL_FAILURE"
                        api.command(account, "DISABLED")
                        append_jsonl(event_path, {"event": "TECHNICAL_FAILURE", "at": sampled_at, **state["identity"], "reason": state["technical_failure"]})
                    if state["stop_reason"] and state["stop_reason"] != "TECHNICAL_FAILURE":
                        api.command(account, "DISABLED")
                        append_jsonl(event_path, {"event": "RUN_FINISHED", "at": sampled_at, **state["identity"], "reason": state["stop_reason"]})
                        state["finish_event_written"] = True
                except (httpx.HTTPError, TimeoutError, RuntimeError, KeyError, ValueError) as exc:
                    state["fail_samples"] += 1
                    if state["disconnect_since"] is None:
                        state["disconnect_since"] = time.time()
                        state["recovery_since"] = state["disconnect_since"]
                        append_jsonl(event_path, {"event": "DISCONNECTED", "at": sampled_at, **state["identity"], "reason": type(exc).__name__})
                    if state["fail_samples"] >= 10:
                        state["technical_failure"] = f"bounded API/runtime recovery exhausted: {type(exc).__name__}"
                        state["stop_reason"] = "TECHNICAL_FAILURE"
                        try:
                            api.command(account, "DISABLED")
                        except (httpx.HTTPError, RuntimeError, TimeoutError):
                            pass
                        append_jsonl(event_path, {"event": "TECHNICAL_FAILURE", "at": sampled_at, **state["identity"], "reason": state["technical_failure"]})
            time.sleep(15)
        for account, state in states.items():
            if state["stop_reason"] is None:
                state["stop_reason"] = "MAX_WALL_CLOCK_REACHED" if time.monotonic() >= wall_deadline else "RUN_FINISHED"
                try:
                    api.command(account, "DISABLED")
                except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
                    append_jsonl(event_path, {"event": "SAFE_STOP_FAILED", "at": utc_now(),
                                              **state["identity"], "error": type(exc).__name__})
                append_jsonl(event_path, {"event": "RUN_FINISHED", "at": utc_now(), **state["identity"], "reason": state["stop_reason"]})
                state["finish_event_written"] = True
            elif not state.get("finish_event_written"):
                append_jsonl(event_path, {"event": "RUN_FINISHED", "at": utc_now(), **state["identity"],
                                          "reason": state["stop_reason"],
                                          "technical_failure": state.get("technical_failure")})
                state["finish_event_written"] = True
        summaries = {str(a): summarize_account(s, args.target_valid_hours, args.max_wall_hours) for a, s in states.items()}
        counts = sum(x["confirmed_gifts"] - x["preexisting_gifts"] for x in summaries.values())
        valid_total = sum(x["valid_online_hours"] for x in summaries.values())
        pooled_rate = counts / valid_total if valid_total else None
        account_by_profile = {v["profile"]: v for v in summaries.values()}
        control = account_by_profile.get("CONTROL", {})
        high = account_by_profile.get("HIGH_ACTIVITY", {})
        ratio = (high.get("moving_seconds_per_hour", 0) / control.get("moving_seconds_per_hour", 1)
                 if control.get("moving_seconds_per_hour") else None)
        result = {"run_id": run_id, "started_at": metadata.get("actual_started_at", metadata["started_at"]),
                  "finished_at": utc_now(), "source_commit": source_commit, "configuration_sha256": config_hash,
                  "accounts": summaries, "high_activity_control_movement_intensity_ratio": ratio,
                  "pooled_gifts_per_valid_online_hour": pooled_rate,
                  "pooled_estimated_valid_hours_per_8_gifts": 8 / pooled_rate if pooled_rate else None,
                  "profile_estimated_valid_hours_per_8_gifts": {
                      k: 8 / v["gifts_per_valid_online_hour"] if v["gifts_per_valid_online_hour"] else None
                      for k, v in summaries.items()}, "weekly_cap_reached": False}
        durable_json(root / "final_summary.json", result)
        (root / "final_summary.md").write_text(render_summary(result))
        metadata.update(status="FINISHED", finished_at=result["finished_at"], final_summary=str(root / "final_summary.json"))
        durable_json(root / "metadata.json", metadata)
        print(json.dumps({"run_id": run_id, "status": "FINISHED", "summary": str(root / "final_summary.md")}), flush=True)
        return 0
    except Exception as exc:
        append_jsonl(event_path, {"event": "RUN_ERROR", "at": utc_now(), "run_id": run_id, "error": type(exc).__name__, "message": str(exc)[:300]})
        for account, state in states.items():
            state["technical_failure"] = f"run supervisor: {type(exc).__name__}"
            state["stop_reason"] = "TECHNICAL_FAILURE"
            try:
                api.command(account, "DISABLED")
            except (httpx.HTTPError, RuntimeError, TimeoutError) as stop_exc:
                append_jsonl(event_path, {"event": "SAFE_STOP_FAILED", "at": utc_now(),
                                          **state["identity"], "error": type(stop_exc).__name__})
            if not state.get("finish_event_written"):
                append_jsonl(event_path, {"event": "RUN_FINISHED", "at": utc_now(), **state["identity"],
                                          "reason": "TECHNICAL_FAILURE", "technical_failure": state["technical_failure"]})
                state["finish_event_written"] = True
        if states:
            partial = {str(a): summarize_account(s, args.target_valid_hours, args.max_wall_hours)
                       for a, s in states.items()}
            durable_json(root / "final_summary.json", {"run_id": run_id, "status": "PARTIAL_TECHNICAL_FAILURE",
                                                          "accounts": partial, "error": type(exc).__name__})
            (root / "final_summary.md").write_text(render_summary({"run_id": run_id,
                "source_commit": source_commit, "configuration_sha256": config_hash,
                "accounts": partial, "pooled_estimated_valid_hours_per_8_gifts": None,
                "high_activity_control_movement_intensity_ratio": None,
                "profile_estimated_valid_hours_per_8_gifts": {}}))
        metadata.update(status="TECHNICAL_FAILURE", error=type(exc).__name__, finished_at=utc_now())
        durable_json(root / "metadata.json", metadata)
        raise
    finally:
        for account, state in states.items():
            if state.get("stop_reason") or state.get("technical_failure"):
                try:
                    api.command(account, "DISABLED")
                except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
                    print(f"Account {account} final safe-stop failed: {type(exc).__name__}", file=sys.stderr)
        for stream in streams.values():
            stream.close()
        api.client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--url")
    parser.add_argument("--accounts", nargs=2, type=int, default=[1, 2])
    duration = parser.add_mutually_exclusive_group(required=False)
    duration.add_argument("--seconds", type=float, help="bounded technical smoke only")
    duration.add_argument("--until-gift", action="store_true", help="explicit full experiment")
    duration.add_argument("--characterize-gifts", action="store_true",
                          help="run fixed-profile repeated-gift characterization")
    parser.add_argument("--max-seconds", type=float, default=604800)
    parser.add_argument("--target-valid-hours", type=float, default=14)
    parser.add_argument("--max-wall-hours", type=float, default=16)
    parser.add_argument("--continue-after-claim", action="store_true")
    parser.add_argument("--preclaim-max-seconds", type=float, default=600,
                        help="bounded preexisting claim preparation per account")
    parser.add_argument("--run-id")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--status", metavar="RUN_ID", help="show current durable run status")
    args = parser.parse_args(argv)
    if args.status:
        return status_characterization(args.status, args.output or Path(".data/gift-characterization"))
    if args.characterize_gifts:
        if not args.continue_after_claim:
            parser.error("--characterize-gifts requires --continue-after-claim")
        if (not 1 <= args.target_valid_hours <= 16 or
                not args.target_valid_hours <= args.max_wall_hours <= 16):
            parser.error("valid target and wall limit must be ordered and no more than 16 hours")
        if not 30 <= args.preclaim_max_seconds <= 600:
            parser.error("preclaim preparation must be bounded to 30–600 seconds")
        if len(set(args.accounts)) != 2:
            parser.error("two distinct accounts are required")
        load_dotenv(args.env_file, override=True)
        api = API(args.url or os.getenv("ORCHESTRATOR_PUBLIC_URL", "http://10.119.21.1:8080"))
        return run_characterization(args, api)
    if args.seconds is None and not args.until_gift:
        parser.error("choose --seconds, --until-gift, --characterize-gifts, or --status")
    seconds = args.max_seconds if args.until_gift else args.seconds
    if len(set(args.accounts)) != 2 or not 1 <= seconds <= 604800:
        parser.error("two distinct accounts and a bounded duration are required")
    load_dotenv(args.env_file, override=True)
    api = API(args.url or os.getenv("ORCHESTRATOR_PUBLIC_URL", "http://10.119.21.1:8080"))
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    root = (args.output or Path(".data/dual-ab")) / run_id
    profiles = dict(zip(args.accounts, ("CONTROL", "HIGH_ACTIVITY"), strict=True))
    identities, finished, final = {}, set(), {}
    streams = {}
    try:
        api.reserve(args.accounts)
        for account in args.accounts:
            runtime = api.start(account)
            api.command(account, "DISABLED")
            session = f"a{account}-r{runtime['runtime_id']}-{uuid.uuid4().hex}"
            identities[account] = {"account_id": account, "runtime_id": runtime["runtime_id"],
                                   "container": runtime["external_id"], "session_id": session,
                                   "profile": profiles[account],
                                   "guest_evidence": f"/home/dst/.local/state/dst-runtime/experiments/{session}"}
            folder = root / f"account-{account}" / session
            folder.mkdir(parents=True)
            streams[account] = (folder / "telemetry.jsonl").open("a")
        if len({i["runtime_id"] for i in identities.values()}) != 2:
            raise RuntimeError("accounts share a runtime")
        durable_json(root / "run.json", {"run_id": run_id, "until_gift": args.until_gift,
                                        "sessions": list(identities.values())})
        for account in args.accounts:
            api.command(account, "ACTIVE", locomotion_profile=profiles[account],
                        experiment_session_id=identities[account]["session_id"],
                        experiment_seconds=seconds, experiment_until_gift=args.until_gift)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and len(finished) < 2:
            for account in args.accounts:
                if account in finished:
                    continue
                status = api.request("GET", f"accounts/{account}/worker")
                report = worker_report(status)
                telemetry = report.get("telemetry", {})
                movement = telemetry.get("locomotion", {})
                if movement.get("session_id") != identities[account]["session_id"]:
                    raise RuntimeError(f"Account {account} experiment session identity lost")
                observation = report.get("details", {}).get("observation", {})
                record = {"sampled_at": datetime.now(UTC).isoformat(), **identities[account],
                          "mode": report.get("mode"), "state": report.get("state"),
                          "last_tick_at": report.get("last_tick_at"), "error": report.get("error_code"),
                          "screen": observation.get("screen"), "telemetry": telemetry}
                streams[account].write(json.dumps(record, sort_keys=True) + "\n")
                streams[account].flush()
                final[account] = record
                if report.get("state") in {"ERROR", "NEEDS_ATTENTION"}:
                    raise RuntimeError(f"Account {account} worker requires recovery: {report.get('error_code')}")
                receipt = telemetry.get("inworld_gift_confirmation")
                if args.until_gift and receipt:
                    if receipt.get("experiment_session_id") != identities[account]["session_id"]:
                        raise RuntimeError("claim receipt session identity mismatch")
                    durable_json(root / f"account-{account}" / identities[account]["session_id"] / "claim.json", receipt)
                    api.command(account, "DISABLED")
                    finished.add(account)
                    print(f"Account {account} gift confirmed; the other account continues", flush=True)
                elif report.get("mode") == "DISABLED":
                    if args.until_gift:
                        raise TimeoutError(f"Account {account} stopped before a confirmed gift")
                    finished.add(account)
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        for account in args.accounts:
            status = api.request("GET", f"accounts/{account}/worker")
            report = worker_report(status)
            final.setdefault(account, {})["final_telemetry"] = report.get("telemetry", {})
        durable_json(root / "result.json", {"run_id": run_id, "gift_completed": sorted(finished)
                                             if args.until_gift else [], "accounts": final})
        print(json.dumps({"run_id": run_id, "evidence": str(root), "accounts": final}, sort_keys=True))
        return 0 if not args.until_gift or len(finished) == 2 else 2
    finally:
        for account in identities:
            try:
                api.command(account, "DISABLED")
            except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
                print(f"Account {account} safe-stop failed: {type(exc).__name__}", file=sys.stderr)
        for stream in streams.values():
            stream.close()
        api.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
