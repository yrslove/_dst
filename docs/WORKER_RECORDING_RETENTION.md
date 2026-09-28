# Worker recording retention

## Current inventory

The live runtime recording directory contained 60 sessions on 2026-09-28. `du` reported about 987 MiB allocated (1,029,871,886 bytes of files). Recording was disabled after the live validation. Each existing session declares limits of 64 MiB, 90 frames, and 180 seconds; no individual session exceeded those limits.

Recorded observation coverage:

| State | Observations |
| --- | ---: |
| MAIN_MENU | 395 |
| HOST_GAME_WORLD_LIST | 62 |
| HOST_GAME_WORLD_SELECTED | 33 |
| CHARACTER_SELECTION | 186 |
| CHARACTER_LOADOUT | 9 |
| IN_WORLD_IDLE | 10 |
| LOGIN_REWARD_AVAILABLE | 41 |
| REWARD_RESULT | 16 |
| OPTIONS | 2 |
| OPTIONS_DISCARD_CONFIRM | 2 |
| UNKNOWN | 283 |

No recording contains `LOADING`, `PAUSED`, or `WORLD_RESET_PENDING`. The existing real-frame samples for those states remain under `runtime_agent/gameworker/dst/assets/samples/` and are the regression fixtures for now.

## Retention set

Keep representative recordings that exercise distinct states and action histories:

- `c86211ad7e714b61a00d75ab671a28e8`: stable MAIN_MENU observations.
- `5b4be5b29077464f9b69d8230e86e676`: Host Game hover/click regression history.
- `604c304b33344543b8b7db3ec23ae866`: HOST_GAME_WORLD_LIST observations.
- `73d4a28853e84fbfabf85289313a5fcc`: selected saved world.
- `71a2633deb774b62a60996e8665e4f1e`: complete CHARACTER_SELECTION history.
- `8af1cf5b817a41ed898b61a184ccb34b`: CHARACTER_LOADOUT and start action history.
- `00b207365b46407a8122c35bd4b059ff`: alive IN_WORLD_IDLE observations.
- `d5796b1b7392485fac284bc4c6828a3c`: multi-state reward/menu/world navigation.
- `d7a9fdf9e8064d78b7d1d824a474f2d5`: Options action and return history.

The remaining sessions have not been proven duplicates: their frame sequences and event histories differ. Five empty sessions contain only 1.5–2.5 KiB each; keeping them preserves small lifecycle traces and saves no meaningful space. No recording was deleted during this audit.

## Future cleanup rule

Classify sessions from `manifest.json` and `events.jsonl` before pruning. Keep at least one complete session for each observed state, one session for each verified action transition, and one fixture for every historical regression. Keep multi-state recordings when they prove sequence order. Prefer complete sessions over interrupted or truncated sessions when they cover the same state and action path. A session can be removed only when its state and action coverage are fully represented by retained sessions and its events add no distinct failure or regression evidence.

This is a per-session storage bound. The recording subsystem does not currently enforce a cumulative directory quota, so repeated sessions can accumulate. Do not add automatic age-only deletion; first add durable retention labels or a state-coverage index that lets cleanup protect the retention set above.
