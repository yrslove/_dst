from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

from PIL import Image

from runtime_agent.gameworker.diagnostics import WorkerDiagnostics
from runtime_agent.gameworker.geometry import NormalizedRegion
from runtime_agent.gameworker.vision import DSTScreen, ObservationValidity
from tests.unit.test_gameworker_perception import make_frame, make_observation


def test_uncertainty_bundle_keeps_context_crops_and_three_following_frames(tmp_path):
    diagnostics = WorkerDiagnostics(
        tmp_path, enabled=True, ring_size=3, max_bytes=4 * 1024 * 1024
    )
    diagnostics.update_context(
        worker_mode="ACTIVE",
        worker_state="OBSERVING",
        task_state="IN_WORLD_IDLE",
        gift_state="GRAY_PENDING",
        held_inputs=False,
    )
    assets = {
        "game_hud": SimpleNamespace(
            threshold=0.94, expected_region=NormalizedRegion(0.1, 0.1, 0.3, 0.3)
        ),
        "player_marker": SimpleNamespace(
            threshold=0.75, expected_region=NormalizedRegion(0.7, 0.1, 0.9, 0.3)
        ),
    }
    calibration = SimpleNamespace(profile_id="dst-test", version=2, verified=True)

    for sequence in range(1, 12):
        frame = make_frame(
            sequence,
            captured_monotonic=10.0 + sequence,
            image=Image.new("RGB", (8, 6), (sequence, 2, 3)),
        )
        observation = make_observation(frame, generation=sequence)
        if sequence < 8:
            observation = replace(observation, screen=DSTScreen.IN_WORLD_IDLE)
        else:
            observation = replace(
                observation,
                validity=ObservationValidity.UNKNOWN,
                screen=DSTScreen.UNKNOWN,
            )
        diagnostics.observe(frame, observation, assets=assets, calibration=calibration)

    bundles = list(tmp_path.glob("evidence-*/manifest.json"))
    assert len(bundles) == 1
    manifest = json.loads(bundles[0].read_text())
    assert manifest["previous_stable_state"] == "IN_WORLD_IDLE"
    assert manifest["selected_state"] == "UNKNOWN"
    assert manifest["thresholds"] == {"game_hud": 0.94, "player_marker": 0.75}
    assert manifest["state"]["worker_mode"] == "ACTIVE"
    assert manifest["state"]["held_inputs"] is False
    assert [item["sequence"] for item in manifest["frames"]] == list(range(1, 12))
    assert (bundles[0].parent / "crops/game_hud.png").is_file()
    assert (bundles[0].parent / "crops/player_marker.png").is_file()


def test_diagnostics_keeps_unknown_real_and_marker_drop_as_development_evidence(
    tmp_path,
):
    diagnostics = WorkerDiagnostics(
        tmp_path, enabled=True, ring_size=2, max_bytes=4 * 1024 * 1024
    )
    assets = {
        "player_marker": SimpleNamespace(
            threshold=0.75, expected_region=NormalizedRegion(0.7, 0.1, 0.9, 0.3)
        )
    }
    calibration = SimpleNamespace(profile_id="dst-test", version=2, verified=True)
    for sequence in range(1, 6):
        frame = make_frame(
            sequence,
            captured_monotonic=10.0 + sequence,
            image=Image.new("RGB", (8, 6), (sequence, 2, 3)),
        )
        observation = make_observation(frame, generation=sequence)
        if sequence == 1:
            observation = replace(observation, screen=DSTScreen.IN_WORLD_IDLE)
        else:
            detections = tuple(
                item.__class__(
                    item.kind,
                    item.detected,
                    0.65 if item.kind == "player_marker" else item.confidence,
                    item.bounds,
                    item.detector_id,
                    item.detector_version,
                    item.template_id,
                    item.verified,
                    item.metadata,
                )
                for item in observation.detections
            )
            observation = replace(
                observation,
                screen=DSTScreen.IN_WORLD_IDLE,
                detections=detections,
            )
        diagnostics.observe(frame, observation, assets=assets, calibration=calibration)

    manifest_paths = list(tmp_path.glob("evidence-*/manifest.json"))
    assert len(manifest_paths) == 1
    manifest = json.loads(manifest_paths[0].read_text())
    assert manifest["selected_state"] == "IN_WORLD_IDLE"
    marker = next(
        item for item in manifest["detectors"] if item["id"] == "player_marker"
    )
    assert marker["score"] == 0.65
    assert marker["threshold"] == 0.75
