"""Confirm an in-world receipt from fresh UI and DST's native item-service ACK."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from runtime_agent.gameworker.vision import DSTScreen, GameObservation

ACK = re.compile(rb"\[SetItemOpened_Complete Success:200\] (\{[^\r\n]+\})")
MAX_LOG_BYTES = 8 * 1024 * 1024


class InWorldClaimEvidence:
    def __init__(self, inventory_before: Path, client_log: Path, result_path: Path):
        self.client_log = client_log
        self.result_path = result_path
        raw = inventory_before.read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("inventory baseline is oversized")
        baseline = json.loads(raw)
        if baseline.get("Error") is not False:
            raise ValueError("inventory baseline has an item-service error")
        self.pending = {
            item["ItemID"]: item["ItemType"]
            for item in baseline["Items"] if item.get("Context") == 3
        }
        self.started_at = inventory_before.stat().st_mtime
        self.baseline_sha256 = hashlib.sha256(raw).hexdigest()
        self.receipt: dict | None = None
        self.pending_close: dict | None = None
        self._world_frames = 0

    def resume_recording(self, recording: Path) -> None:
        """Recover verification only; recorded inputs are never replayed."""
        manifest = json.loads((recording / "manifest.json").read_text())
        if (manifest.get("worker_mode") != "ACTIVE"
                or manifest.get("capture_source", {}).get("kind") != "x11-pillow"):
            raise ValueError("verification recovery requires a live ACTIVE recording")
        raw = (recording / "events.jsonl").read_bytes()
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("claim recording is oversized")
        observations = {}
        pending = None
        for line in raw.splitlines():
            event = json.loads(line)
            payload = event["payload"]
            if event["event_type"] == "OBSERVATION_PRODUCED":
                observations[event["frame_id"]] = payload["observation"]
            result = payload.get("result", {})
            if event["event_type"] != "ACTION_RESULT":
                continue
            if pending and result.get("action_id") == pending["action_id"]:
                continue
            if result.get("status") in {"SUPPRESSED", "REJECTED"}:
                continue
            pending = None
            if result.get("status") != "VERIFYING":
                continue
            if result.get("action") != "CLICK_INWORLD_USE_LATER":
                continue
            source = observations.get(event["frame_id"], {})
            anchors = {d["kind"] for d in source.get("detections", [])
                       if d.get("detected") and d.get("verified") and d.get("confidence", 0) >= .94}
            if not (
                source.get("screen") == "IN_WORLD_GIFT_RECEIVED"
                and source.get("validity") == "VALID"
                and source.get("assets_verified") is True
                and source.get("calibration_verified") is True
                and source.get("runtime_id") == manifest.get("runtime_id")
                and source.get("runtime_generation") == manifest.get("runtime_generation")
                and source.get("screen_confidence", 0) >= .94
                and {"inworld_gift_received_title", "inworld_gift_use_later", "inworld_gift_use_now"} <= anchors
            ):
                continue
            sent_at = datetime.fromisoformat(event["timestamp"]).timestamp()
            if not 0 <= time.time() - sent_at <= 600:
                continue
            pending = {
                "received_frame_id": source["source_frame_id"],
                "received_sequence": source["source_sequence"],
                "received_at": source["timestamp"],
                "received_worker_generation": source["worker_generation"],
                "runtime_generation": source["runtime_generation"],
                "runtime_id": source["runtime_id"],
                "action_id": result["action_id"],
                "close_sent_at": event["timestamp"],
                "verification": "RECORDED_CANONICAL_CLOSE_FRESH_WORLD",
                "recording_sha256": hashlib.sha256(raw).hexdigest(),
            }
        if not pending:
            raise ValueError("no bounded verified canonical receipt close to resume")
        self.pending_close = pending

    def observe(self, observation: GameObservation) -> dict | None:
        if not self.pending_close:
            return None
        if not (
            observation.production_ready and observation.is_fresh()
            and observation.screen == DSTScreen.IN_WORLD_IDLE
            and observation.screen_confidence >= .94
            and observation.runtime_generation == self.pending_close["runtime_generation"]
            and observation.runtime_id == self.pending_close["runtime_id"]
            and not any(d.detected and d.verified for d in observation.detections
                        if d.kind in {"inworld_gift_received_title", "inworld_gift_use_later"})
        ):
            self._world_frames = 0
            return None
        self._world_frames += 1
        if self._world_frames < 2:
            return None
        return self.confirm({
            **self.pending_close,
            "evidence_frame_id": observation.source_frame_id,
            "evidence_sequence": observation.source_sequence,
            "observed_at": observation.timestamp,
            "worker_generation": observation.worker_generation,
        })

    def confirm(self, closed: dict) -> dict | None:
        if self.receipt:
            return self.receipt
        if not (
            isinstance(closed.get("received_sequence"), int)
            and isinstance(closed.get("evidence_sequence"), int)
            and (closed.get("received_worker_generation", 1) != closed.get("worker_generation", 1)
                 or closed["received_sequence"] < closed["evidence_sequence"])
            and closed.get("action_id")
            and closed.get("received_frame_id")
            and closed.get("evidence_frame_id")
        ):
            return None
        observed = datetime.fromisoformat(closed["observed_at"]).timestamp()
        received = datetime.fromisoformat(closed["received_at"]).timestamp()
        if not self.started_at <= received <= observed:
            return None
        try:
            with self.client_log.open("rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                stream.seek(max(0, size - MAX_LOG_BYTES))
                offset = stream.tell()
                tail = stream.read(MAX_LOG_BYTES)
        except OSError:
            return None
        for match in reversed(list(ACK.finditer(tail))):
            try:
                ack = json.loads(match.group(1))
            except ValueError:
                continue
            item_id = ack.get("ItemID")
            modified = ack.get("Modified")
            if not (
                ack.get("Error") is False
                and isinstance(item_id, int)
                and item_id in self.pending
                and isinstance(modified, (int, float))
                and self.started_at <= modified <= observed + 60
            ):
                continue
            receipt = {
                **closed,
                "semantic": "IN_WORLD_GIFT_CONFIRMED",
                "item_id": item_id,
                "item_type": self.pending[item_id],
                "backend": {
                    "operation": "SetItemOpened_Complete",
                    "http_status": 200, "error": False, "modified": modified,
                    "client_log_offset": offset + match.start(),
                    "ack_sha256": hashlib.sha256(match.group()).hexdigest(),
                },
                "inventory_before_sha256": self.baseline_sha256,
            }
            self.result_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.result_path.with_suffix(".tmp")
            with temporary.open("w") as stream:
                json.dump(receipt, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.result_path)
            directory = os.open(self.result_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.receipt = receipt
            return receipt
        return None
