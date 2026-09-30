# AFK Gift Observation — 2026-09-30

This log tracks the live safe-world AFK and gift experiment. Times are UTC.
Update it at each live checkpoint; do not treat a worker pause as a DST restart.

## Session

- Runtime: `dst-000001-g1`, runtime generation 1.
- DST process generation: `r1-g1-p1026-t6884012`, PID 1026.
- DST process start: `2026-09-30 02:22:20.146233 UTC`.
- World session: `EA6E12E4296C650B`; save path advanced to `0000000036` by `13:21 UTC`.
- World profile: `WORLD_PROFILE_VERIFIED` at `13:21 UTC`; `day=onlyday`, profile hash and loaded world verified. `world_behavior_verified=false` (profile application only).
- Exact world-entry timestamp: not yet recovered from the available worker history.
- First visually verified AFK world checkpoint in this observation run: approximately `02:46:30 UTC`, 1280x720 Xpra screenshot. Wilson was alive at Florid Postern; day 3, hotbar, and survival HUD visible; no menu, death/reset, loading, or modal.
- Worker reported `UNKNOWN` around `02:28 UTC` while paused in `OBSERVE`, with no held input or worker actions. Both retained real marker-failure frames were visually classified as ordinary `IN_WORLD_IDLE`; exact frame identity for the first reported transition remains unavailable.
- At `12:34 UTC`, the worker was still `DISABLED`, with no input owner. It was returned through Control Plane to `OBSERVE`, then `ACTIVE` only after a fresh valid in-world observation. No DST/world restart occurred; Xvfb, Steam, and DST remained alive.
- **Verified post-fix stable AFK window:** `12:35:15.503636` to `13:23:21.688042 UTC` (48m06s). At the final checkpoint the canonical worker had 236 successful captures and 236 successful perception passes, with 0 capture errors, 0 perception errors, 0 behavior `unknown_frames`, 0 worker actions, and `held_inputs=false`. Screen remained `IN_WORLD_IDLE / VALID` throughout observed checkpoints.
- A read-only full framebuffer capture at `13:21:53 UTC` was visually inspected: alive character, day 33 clock, hotbar and survival HUD; no menu, death/reset, loading, or modal. This and the earlier `02:46` screenshot bracket a long gap without canonical observation. Do not count that gap as continuously verified perception stability or AFK time.
- No movement input or gameplay action was sent after the first verified AFK checkpoint. Within the verified 48m06s window, cumulative confirmed AFK time is 48m06s; exact world-entry time and movement before that checkpoint are unknown.

## Gift observations

| Time UTC | State | Detector evidence | Screenshot |
|---|---|---|---|
| ~02:46:30 | Gray/inactive gift icon; no ACTIVE gift | `world_present_banner=0.999937`; `gift_icon` confidence `0.999937`, `icon_state=INACTIVE`, `availability=NO_REWARD_AVAILABLE`, `chroma_p95=13`, `colored_fraction=0` | `runtime_agent/gameworker/dst/assets/samples/in_world_marker_false_negative_live_20260930_0246.png` |
| 12:35:15–13:23:21 | Gray/inactive throughout verified AFK window | `NO_REWARD_AVAILABLE`; icon identity confidence `0.999937`, bounds `[0.13125, 0.01389, 0.17656, 0.09444]`, `chroma_p95=13`, `colored_fraction=0` | Read-only 1280x720 framebuffer visually checked at start and at 13:21; end image `/tmp/final-afk-check.png` (temporary) |

- First ACTIVE timestamp: not observed in 48m06s of continuously verified AFK; no conclusion yet about a longer AFK-only delay.
- `CLICK_GIFT_ICON`: not performed. `LOGIN_REWARD_AVAILABLE`: not observed. Claim action: not performed. `DAILY_GIFT_CONFIRMED`: not reached.
- Worker stayed `ACTIVE / WAITING` on valid in-world frames, so the canonical gift flow was available; there was no ACTIVE sample to hand to it.
- No pause interrupted the verified window. Earlier uncertainty diagnostics paused/disabled worker observation while DST stayed alive; that unobserved interval is excluded from the confirmed AFK total.

## Perception result

- The 2 real false-negative fixtures now replay as `IN_WORLD_IDLE`: one with the reported production marker score `0.657998`, and one captured at `02:46 UTC` whose local replay marker score is `0.5540` (the visible white overlay over the heart defeats the static red-heart template). The older variation fixture replays at `0.763` against `0.75`.
- In-world fallback contract uses independent `game_hud` and `world_present_banner` evidence after high-priority loading, reset, main-menu, modal, pause, and reward checks. The player marker can reinforce the result but cannot alone reject an otherwise clear live HUD. Missing/contradictory anchors still permit `UNKNOWN`.
- Fixture corpus replay and focused unit tests cover the retained in-world, marker-variation, gray-gift, reset-pending, main-menu, loading, paused, modal, reward-result, and unknown cases. Coverage still lacks retained real `DEAD`, `LOGIN_REWARD_AVAILABLE`, `NO_REWARD_AVAILABLE` result, and visually reviewed `UNKNOWN_REAL` screenshots; do not claim those live cases proven.
- Post-fix live result: 48m06s, 236/236 successful frame capture/perception counts, zero UNKNOWNs, zero obvious false-positive IN_WORLD, zero held inputs. This is replay/unit plus live-world evidence; it does not replace missing real negative-state fixtures.
