"""Small HUD-only gift evidence; colored references must be independently verified."""

from __future__ import annotations

import numpy as np

from runtime_agent.gameworker.geometry import Viewport

GIFT_ROI = (0.115, 0.0, 0.36, 0.14)
HOVER_ROI = (0.10, 0.10, 0.32, 0.23)
UNKNOWN = "GIFT_AVAILABILITY_UNKNOWN"
PENDING = "IN_WORLD_GIFT_PENDING"


def classify_icon(image, detection, template, *, active=None, hover_verified=False):
    """Use located icon pixels, excluding the world and the template border."""
    evidence = {
        "icon_state": "UNKNOWN",
        "availability": UNKNOWN,
        "hover_verified": hover_verified,
        "active_reference_verified": bool(active and active.verified),
    }
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
