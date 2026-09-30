"""Bounded production GameWorker invocation on an existing verified display.

This entrypoint owns no game, Steam, display, or Runtime Agent lifecycle.
The caller must suspend the old input owner before handing over the display.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from app.runtime.display import DisplayEnvironment
from runtime_agent.gameworker.base import WorkerContext
from runtime_agent.gameworker.config import WorkerConfig, WorkerMode
from runtime_agent.gameworker.dst.worker import DSTGameWorker
from runtime_agent.gameworker.reward_evidence import InWorldClaimEvidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True, type=int)
    parser.add_argument("--runtime-id", required=True, type=int)
    parser.add_argument("--worker-generation", required=True, type=int)
    parser.add_argument("--display", default=":99")
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--seconds", type=float, default=45)
    parser.add_argument("--active", action="store_true")
    parser.add_argument("--inventory-before", type=Path)
    parser.add_argument("--client-log", type=Path)
    parser.add_argument("--resume-recording", type=Path)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 180:
        parser.error("seconds must be in [1, 180]")
    logging.basicConfig(level=logging.INFO)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    config = WorkerConfig(
        plugin="dst", profile="dst-1280x720-linux-v1",
        mode=WorkerMode.ACTIVE if args.active else WorkerMode.OBSERVE,
        calibration_verified=True, input_subprocess_timeout=5,
        recording_enabled=True, recording_root=args.evidence_dir / "recordings",
        recording_max_duration=args.seconds + 20,
        diagnostic_directory=args.evidence_dir / "diagnostics",
    )
    config.validate()
    context = WorkerContext(
        args.account_id, args.runtime_id,
        DisplayEnvironment(args.display, "/run/dst-runtime"),
        runtime_verified=True,
    )
    if bool(args.inventory_before) != bool(args.client_log):
        parser.error("inventory-before and client-log must be supplied together")
    claim_evidence = (
        InWorldClaimEvidence(args.inventory_before, args.client_log,
                             args.evidence_dir / "inworld-gift-confirmed.json")
        if args.inventory_before else None
    )
    if args.resume_recording:
        if claim_evidence is None:
            parser.error("resume-recording requires item-service evidence")
        claim_evidence.resume_recording(args.resume_recording)
    worker = DSTGameWorker(config, worker_generation=args.worker_generation,
                           claim_evidence=claim_evidence)
    deadline = time.monotonic() + args.seconds
    try:
        worker.prepare(context)
        worker.on_game_ready(context)
        last = None
        with (args.evidence_dir / "reports.jsonl").open("a") as stream:
            while time.monotonic() < deadline:
                report = worker.tick(context).as_dict()
                observed = report.get("last_observation_at")
                if observed != last:
                    stream.write(json.dumps(report, sort_keys=True) + "\n")
                    stream.flush()
                    last = observed
                (args.evidence_dir / "status.json").write_text(json.dumps(report))
                if report["state"] in {"ERROR", "NEEDS_ATTENTION"}:
                    break
                if report["telemetry"].get("inworld_gift_confirmation"):
                    break
                time.sleep(0.1)
    finally:
        final = worker.shutdown().as_dict()
        (args.evidence_dir / "final.json").write_text(json.dumps(final))
    print(json.dumps(final))
    return 0 if final["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
