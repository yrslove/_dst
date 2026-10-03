"""Small HUD-only gift evidence; colored references must be independently verified."""

from __future__ import annotations

import time
from collections import deque

import numpy as np

from runtime_agent.gameworker.geometry import NormalizedRegion, Viewport

GIFT_ROI = (0.035, 0.0, 0.36, 0.14)
HOVER_ROI = (0.10, 0.10, 0.32, 0.23)
UNKNOWN = "GIFT_AVAILABILITY_UNKNOWN"
PENDING = "IN_WORLD_GIFT_PENDING"


class GiftTemporalEvidence:
    """Debounce the existing HUD detector without making the HUD authoritative."""

    def __init__(self):
        self.state = "ABSENT"
        self.frames = 0
        self.first_transient_visual_at = None
        self.confirmed_persistent_visual_at = None
        self.confirmed_first_detection = None
        self.last_frame = None
        self.last_observed_monotonic = None
        self.before_frame = None
        self.visible_frame = None
        self.started = None
        self.pending_before = None
        self.episode_first_visual_at = None
        self.episode_confirmed_at = None
        self.confirmed_receipt_logged = False
        self.context = {}
        self.events = deque(maxlen=16)
        self.event_sequence = 0

    @property
    def candidate(self):
        return self.state == "PERSISTENT"

    def observe(self, observation, *, pending=None, receipt=None):
        if (observation is None or observation.source_frame_id == self.last_frame
                or not observation.is_fresh() or observation.screen.value != "IN_WORLD_IDLE"):
            return
        icon = next((d for d in observation.detections if d.kind == "gift_icon"), None)
        visible = bool(icon and icon.detected and icon.verified and icon.confidence >= .94
                       and dict(icon.metadata).get("availability") in {
                           "GIFT_AVAILABLE", "IN_WORLD_GIFT_PENDING"})
        previous_frame = self.last_frame
        previous_at = self.last_observed_monotonic
        self.last_frame = observation.source_frame_id
        observed_monotonic = getattr(observation, "observed_monotonic", time.monotonic())
        self.last_observed_monotonic = observed_monotonic
        if visible:
            if (self.state != "ABSENT" and previous_at is not None
                    and observed_monotonic - previous_at > 5.0):
                self.state = "ABSENT"
                self.frames = 0
            if self.state == "ABSENT":
                self.before_frame = previous_frame
                self.visible_frame = observation.source_frame_id
                self.started = observed_monotonic
                self.pending_before = pending
                self.episode_first_visual_at = observation.timestamp
                self.episode_confirmed_at = None
                self.confirmed_receipt_logged = False
                self.frames = 0
                if self.first_transient_visual_at is None:
                    self.first_transient_visual_at = observation.timestamp
            self.frames += 1
            self.state = "SEEN_ONCE"
            if self.frames >= 3:
                self.state = "PERSISTENT"
                self.episode_confirmed_at = observation.timestamp
                if self.confirmed_first_detection is None:
                    self.confirmed_first_detection = observation.timestamp
                if self.frames >= 3 and self.confirmed_persistent_visual_at is None:
                    self.confirmed_persistent_visual_at = observation.timestamp
            if receipt and not self.confirmed_receipt_logged:
                self.event_sequence += 1
                self.events.append({
                    **self.context, "event_id": self.event_sequence,
                    "event": "STALE_GIFT_HUD_AFTER_CONFIRMED_CLAIM",
                    "timestamp": observation.timestamp,
                    "account_id": self.context.get("account_id"),
                    "frames_visible": self.frames,
                    "first_transient_visual_at": self.episode_first_visual_at,
                    "confirmed_persistent_visual_at": self.episode_confirmed_at,
                    "after_frame_id": observation.source_frame_id,
                    "SetItemOpened_Complete": True, "receipt": True,
                })
                self.confirmed_receipt_logged = True
            return
        if self.state != "ABSENT":
            self.event_sequence += 1
            event = {
                **self.context, "event_id": self.event_sequence,
                "event": ("STALE_GIFT_HUD_AFTER_CONFIRMED_CLAIM" if receipt else
                          "GIFT_VISUAL_FLASH" if self.frames < 3 else
                          "GIFT_VISUAL_DISAPPEARED_UNCONFIRMED"),
                "timestamp": observation.timestamp, "account_id": self.context.get("account_id"),
                "frames_visible": self.frames,
                "first_transient_visual_at": self.episode_first_visual_at,
                "confirmed_persistent_visual_at": self.episode_confirmed_at,
                "duration_ms": round((observed_monotonic - self.started) * 1000),
                "before_frame_id": self.before_frame, "visible_frame_id": self.visible_frame,
                "after_frame_id": observation.source_frame_id,
                "item_service_pending_before": self.pending_before,
                "item_service_pending_during": self.context.get("pending_during"),
                "item_service_pending_after": pending,
                "SetItemOpened_Complete": bool(receipt), "receipt": bool(receipt),
            }
            self.events.append(event)
        self.state = "ABSENT"
        self.frames = 0

    def telemetry(self):
        return {"state": self.state, "frames_visible": self.frames,
                "first_transient_visual_at": self.first_transient_visual_at,
                "confirmed_persistent_visual_at": self.confirmed_persistent_visual_at,
                "confirmed_first_detection": self.confirmed_first_detection,
                "events": list(self.events)}


def classify_icon(image, detection, template, *, active=None, hover_verified=False):
    """Use located icon pixels, excluding the world and the template border."""
    evidence = {
        "icon_state": "UNKNOWN",
        "availability": UNKNOWN,
        "hover_verified": hover_verified,
        "active_reference_verified": bool(active and active.verified),
    }
    if (
        detection.verified
        and detection.detector_id == "opencv-template"
        and not detection.detected
        and detection.bounds is not None
        and template is not None
    ):
        roi = np.asarray(
            image.crop(
                Viewport(*image.size).region(NormalizedRegion(*GIFT_ROI))
            ).convert("L")
        )
        quiet_tiles = 0
        tile_count = 0
        for y in range(0, roi.shape[0] - 15, 16):
            for x in range(0, roi.shape[1] - 15, 16):
                tile_count += 1
                quiet_tiles += int(float(roi[y:y + 16, x:x + 16].std()) < 2.0)
        if tile_count and quiet_tiles / tile_count > 0.40:
            evidence["negative_evidence"] = "HUD_ROI_OBSCURED"
            return evidence
        # Called only for a fresh, valid IN_WORLD_IDLE observation. A verified
        # negative template match means the HUD ROI was inspected and the icon
        # is absent; missing/failed detector assets remain UNKNOWN.
        evidence.update(
            icon_state="ABSENT",
            availability="NO_REWARD_AVAILABLE",
            negative_evidence="VERIFIED_TEMPLATE_ABSENCE",
        )
        return evidence
    if (
        not detection.verified
        or detection.bounds is None
        or detection.confidence < 0.85
    ):
        return evidence
    crop = np.asarray(image.crop(Viewport(*image.size).region(detection.bounds)))
    if crop.shape[:2] != template.shape or crop.ndim != 3:
        return evidence
    mask = (template >= 35) & (template <= 135)
    mask[:3] = mask[-3:] = False
    mask[:, :3] = mask[:, -3:] = False
    if np.count_nonzero(mask) < 100:
        return evidence
    pixels = crop[mask].astype(np.int16)
    chroma = pixels.max(axis=1) - pixels.min(axis=1)
    p95 = float(np.quantile(chroma, 0.95))
    colored_fraction = float(np.mean(chroma > 30))
    evidence.update(chroma_p95=p95, colored_fraction=colored_fraction)
    identity = detection.confidence >= 0.94 or hover_verified
    active_verified = bool(
        active and active.detected and active.verified and active.confidence >= 0.94
    )
    if identity and not active_verified and p95 <= 18 and colored_fraction <= 0.02:
        # DST renders the toast for a pending gift before its nearby giftmachine
        # is enabled. A gray visible toast means a gift exists but is not yet
        # actionable; it is not evidence that the account has no reward.
        evidence.update(icon_state="PENDING", availability=PENDING)
    elif (
        identity
        and active is not None
        and active.detected
        and active.verified
        and active.confidence >= 0.94
        and active.bounds == detection.bounds
        and p95 >= 35
        and colored_fraction >= 0.20
    ):
        evidence.update(icon_state="ACTIVE", availability="GIFT_AVAILABLE")
    return evidence


def hover_response(before, after):
    """Require added text-like light strokes in the localized tooltip area.

    A general world change or cursor movement alone is insufficient. This is
    secondary evidence and never supplies an availability state.
    """
    import cv2

    a = np.asarray(before.convert("L"), dtype=np.int16)
    b = np.asarray(after.convert("L"), dtype=np.int16)
    if a.shape != b.shape:
        return False
    added = ((b >= 160) & (b - a >= 45)).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(added)
    strokes = sum(
        2 <= w <= 25 and 4 <= h <= 35 and 5 <= area <= 300
        for _, _, w, h, area in stats[1:count]
    )
    return bool(np.count_nonzero(added) >= 100 and strokes >= 6)
