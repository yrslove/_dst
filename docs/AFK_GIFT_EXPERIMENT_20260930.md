# AFK Gift Observation — 2026-09-30

This log tracks the live safe-world AFK and gift experiment. Times are UTC.
Update it at each live checkpoint; do not treat a worker pause as a DST restart.

## Session

- Runtime: `dst-000001-g1`, runtime generation 1.
- DST process generation: `r1-g1-p1026-t6884012`, PID 1026.
- DST process start: `2026-09-30 02:22:20.146233 UTC`.
- World session: `EA6E12E4296C650B`; save path ends in `0000000007`.
- World profile: `WORLD_PROFILE_VERIFIED` at `02:57:52 UTC`; `day=onlyday`, profile hash verified, loaded world verified. `world_behavior_verified=false` (profile application only).
- Exact world-entry timestamp: not yet recovered from the available worker history.
- First visually verified AFK world checkpoint in this observation run: approximately `02:46:30 UTC`, 1280x720 Xpra screenshot. Wilson was alive at Florid Postern; day 3, hotbar, and survival HUD visible; no menu, death/reset, loading, or modal.
- Worker status at `02:28:00 UTC`: `PAUSED / OBSERVE`, observation `UNKNOWN`, held inputs false, zero worker actions. Visual replay of the associated marker-failure class establishes that this detector result is a false negative; exact screenshot-to-observation frame identity is not available.
- No movement input or gameplay action was sent by this observation run.
- DST, Runtime Agent, and Xvfb remained running. Xpra shadow became unavailable near `02:58 UTC`; no process restart was attempted. A fresh canonical worker observation is needed to resume visual checkpoints.

## Gift observations

| Time UTC | State | Detector evidence | Screenshot |
|---|---|---|---|
| ~02:46:30 | Gray/inactive gift icon; no ACTIVE gift | `world_present_banner=0.999937`; `gift_icon` confidence `0.999937`, `icon_state=INACTIVE`, `availability=NO_REWARD_AVAILABLE`, `chroma_p95=13`, `colored_fraction=0` | `runtime_agent/gameworker/dst/assets/samples/in_world_marker_false_negative_live_20260930_0246.png` |

- First ACTIVE timestamp: not observed.
- `CLICK_GIFT_ICON`, reward UI, claim action, and `DAILY_GIFT_CONFIRMED`: not performed.
- Cumulative confirmed AFK time starts from the first verified in-world checkpoint above; the exact entry time remains unresolved.
