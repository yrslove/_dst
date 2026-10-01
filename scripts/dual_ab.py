#!/usr/bin/env python3
"""Run two isolated canonical workers. A full run requires explicit --until-gift."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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


def worker_report(status):
    return status.get("details", {}).get("diagnostics", {}).get("worker", {})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--url")
    parser.add_argument("--accounts", nargs=2, type=int, default=[1, 2])
    duration = parser.add_mutually_exclusive_group(required=True)
    duration.add_argument("--seconds", type=float, help="bounded technical smoke only")
    duration.add_argument("--until-gift", action="store_true", help="explicit full experiment")
    parser.add_argument("--max-seconds", type=float, default=604800)
    parser.add_argument("--output", type=Path, default=Path(".data/dual-ab"))
    args = parser.parse_args(argv)
    seconds = args.max_seconds if args.until_gift else args.seconds
    if len(set(args.accounts)) != 2 or not 1 <= seconds <= 604800:
        parser.error("two distinct accounts and a bounded duration are required")
    load_dotenv(args.env_file, override=True)
    api = API(args.url or os.getenv("ORCHESTRATOR_PUBLIC_URL", "http://10.119.21.1:8080"))
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    root = args.output / run_id
    profiles = dict(zip(args.accounts, ("CONTROL", "HIGH_ACTIVITY"), strict=True))
    identities, finished, final = {}, set(), {}
    streams = {}
    try:
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
