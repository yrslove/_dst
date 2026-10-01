# Two-account locomotion A/B

The launcher uses the existing Control Plane lifecycle and GameWorker command/ACK
path. Account 1 uses CONTROL; Account 2 uses HIGH_ACTIVITY. Each session gets an
independent ID, telemetry file, native inventory baseline, detection evidence, and
durable claim receipt. The two accounts must already have separate provisioned,
authenticated, verified runtimes. Bootstrap and remote views derive each account's
DISPLAY from the configured base (`:99` for account 1, `:100` for account 2).

Bounded technical smoke:

```bash
.venv/bin/python scripts/dual_ab.py --env-file .data/linux-validation.env --seconds 120
```

Legacy one-gift smoke, stopping each account independently after its own first
confirmed in-world gift:

```bash
.venv/bin/python scripts/dual_ab.py --env-file .data/linux-validation.env --until-gift
```

Fixed-profile sequential gift characterization (14 valid-online hours per account,
16-hour wall-clock safety limit):

```bash
.venv/bin/python scripts/dual_ab.py --env-file .data/linux-validation.env \
  --characterize-gifts --target-valid-hours 14 --max-wall-hours 16 \
  --continue-after-claim
```

Run it under the host's systemd supervisor (`systemd-run`) so it survives SSH and
Codex session closure. It first captures a fresh gift HUD/item-service baseline;
verified preexisting gifts are claimed through the production worker flow before
measurement and tagged `PREEXISTING_AT_RUN_START`. Each subsequent confirmed gift
gets its own guest receipt, evidence directory, gift row and append-only events; the
same fixed profile continues after every claim. Worker valid time is counted only
from fresh `IN_WORLD_IDLE` observations. Loading, death, disconnect, stale frames,
and recovery gaps do not count. Resource samples are taken every five minutes and
telemetry every fifteen seconds. Source commit and a hash of profiles, limits,
runtime revisions and baseline state are frozen in run metadata.

Run artifacts are stored under `.data/gift-characterization/<RUN_ID>/`. Inspect a
live or completed run with:

```bash
.venv/bin/python scripts/dual_ab.py --status <RUN_ID>
```

On completion, the launcher writes `final_summary.json` and `final_summary.md`,
including per-account intervals/rates, A/B movement ratio and observed pooled
valid-hours projection for eight weekly gifts. High CPU alone does not stop the
run; a new runtime OOM, sustained worker loss, ten-minute failed recovery, or
material host swap thrashing stops only the affected account where possible.

Neither importing the module nor running it without a duration flag starts a run.
The launcher never provisions additional accounts or implements a farm scheduler.
It stops worker input in `finally`; Steam/DST remain running. A runtime guard also
stops a profile at its configured deadline even if the launcher disappears.

CONTROL alternates 0.4-second forward/backward pulses with 18-second idle periods.
HIGH_ACTIVITY uses 1.6-second forward/backward pulses with 0.5-second idle periods.
All actions use the existing canonical input, fresh visual verification, and
recovery path. No gathering, combat, crafting, target search, or pathfinding is
part of either profile. Loading, death/reset, menus, stale frames, and observation
gaps are excluded from valid online activity time. Manual pauses remain paused;
runtime readiness loss releases input and resumes the prior owner when readiness
returns. Three consecutive unverified movements stop the worker for attention.

`telemetry.locomotion` includes movement_commands, moving_seconds, idle_seconds,
direction_changes, active_elapsed, and valid_online_world_elapsed. Commands count
actual sent pulses; moving seconds credit their bounded hold duration only after
fresh world verification. Compare per-minute intensity using valid online world
time. Existing full-scene perception and recording remain bounded; profiles add
no new scene-understanding system.

The native item-service cache is read separately inside each guest. An unopened
Context 3 item arms the existing claim verifier before the production gift click.
A receipt requires the matching Klei user, item ID, successful
SetItemOpened_Complete ACK, and fresh verified receipt-close/world evidence.
Receipts are flushed/fsynced atomically. A completed account stops its own input;
the other account continues. Evidence lives under each guest's
`/home/dst/.local/state/dst-runtime/experiments/<session-id>` and host telemetry
under `.data/dual-ab/<run-id>/account-<id>/<session-id>`; these paths are not committed.

Before starting profiles, the launcher persistently pauses the existing automatic
account scheduler through `POST /api/v1/accounts/<id>/schedule/pause` and waits for
existing jobs to drain. Queued automatic jobs are cancelled; running jobs retain
ownership until completion. The pause does not stop containers or clients and is
not automatically removed on exit, preventing an unexpected subsequent long run.

Fresh verified gift HUD evidence can arm the native verifier when the inventory
cache lags behind the server. Native ACKs and received-screen evidence are durable
before receipt closure. If a managed client reload interrupts that closure, the
same session can finish from its archived native ACK, prior real received-screen
evidence, and two distinct fresh world observations, without opening the item again.
Diagnostic PNGs remain lossless and use fast compression to avoid stalling the
worker's fresh observations during recovery.
