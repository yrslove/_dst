# Current State

## Stationary liveness and reload — live PASS (2026-10-02)

User scope: preserve stationary architecture and zero movement; separate liveness
from gameplay activity; run only a 15–20-minute validation and stop. No long run
was launched. Operator placement beside an existing machine remains precondition.

Exact historical failure condition: WorkerProcessHost.shutdown joined A1's child
for stop_timeout=3s; the child remained alive, so forced=True, _force_stop_child
and _mark_crashed produced healthy=False. Runtime Agent then took its full shutdown
reload fallback. Journal: STOP received 22:06:53, fallback at 22:06:56 UTC. This
happened during deployment shutdown, before stationary wait: the last A1 sample
was MAIN_MENU/JOIN_WORLD with no wait timestamp. It was not an inactivity timer.
The historical log has no stack trace explaining the internal STOP delay; do not
claim a particular IPC/cleanup operation was proven responsible.

Changes are limited to process.py, dst/worker.py, lifecycle tests and this document.
Bounded cooperative STOP grace is now 10s, with explicit worker_stop_timeout logs.
Forced stop and nonzero process exit remain unhealthy. Unexpected process death
is unhealthy during restart backoff; restart budgets and readiness deadline remain.
The existing bounded status-Queue feeder handling is retained.

Worker loop reports carry producer monotonic time; host marks reports older than
30s WORKER_HEARTBEAT_STALE. Command ACKs do not renew the loop timestamp.
Active/observe DST health independently checks last successful capture and last
valid observation, with a 30s freshness limit/startup grace. Fatal states, cleanup
failure and stationary session-loss blockers remain unhealthy. Disabled, explicit
pause and shutdown do not require active capture. Movement, gift arrival, clicks,
proposal count and state transitions are never required for health.

Timer audit: startup readiness 10s (unchanged), cooperative STOP 10s; loop/capture/
valid observation health 30s. Existing capture/perception/planner operation limits
and frame/observation acceptance ages are unchanged. Existing unknown-state
recovery remains 20s. Input deadman checks only held inputs, never idle activity.
Action/transition deadlines apply only to actions actually sent.

Live snapshot 7b86f24caa8f38b4072838c2cbc9874c37d013ce deployed on both guests.
Run stationary_health_smoke_20261002T224618Z: A1 902.796s, A2 901.129s observed
stationary intervals; movement_count=0 and locomotion_commands=0 on both.
All 164/162 stationary samples healthy=True, with 107/106 distinct fresh HUD
samples and capture deltas 2614/2525. Max loop-report ages 10.439/8.292s.
Both remained IN_WORLD_IDLE/GAME_READY with restart_count=0, no recovery blocker,
and unchanged game PIDs 2959/1160. idle_timeout=0 rechecked after world observation.
No gift appeared. Both were disabled/input released at completion, 23:03:24 UTC.

A separate planned post-smoke A1 reload also passed: managed processes preserved,
actual DST PID 2959 unchanged, no stop timeout/full shutdown fallback, GAME_READY,
healthy worker and DISABLED/input released afterward (23:03:51 UTC).
Evidence, reports, copied guest logs and source provenance are preserved under
ignored .data/stationary-health/. Schedules remain paused; no root-checkout commit
or push. Previous root changes and smoke artifacts were preserved.

Tests cover eight modeled stationary hours with fresh HUD/no actions; capture/HUD
staleness independently; loop-report timeout; bounded clean/forced STOP; abnormal
exit and restart backoff. Initial focused suite: 89 passed. Under live load, extended
suite had 171 passes and one NOOP startup test exceeding its explicit 5s test
startup deadline; isolated retry passed in 1.94s. A wider hardening run also exposed
an existing unrelated Xpra argv expectation failure; its path was not changed.
Final six-file focused suite: 172 passed (one existing Starlette deprecation
warning), with workers disabled. Ruff and git diff --check passed. Final API
check confirms both DISABLED, healthy=True, held_inputs=false, driver_present=false.
READY_FOR_LONG_TEST: YES, for an explicit operator launch; none was started.

## Stationary smoke — PASS_WITH_LIMITATIONS, stopped (2026-10-02)

Current user instruction explicitly excludes Science Machine bootstrap/discovery,
initial positioning, anchor calibration and automatic position correction. The
operator places each account next to its existing Science Machine before takeover;
this is a launch precondition, not an autonomous-positioning blocker. Prior-turn
bootstrap edits were reverted. No bootstrap or station search is invoked.

Production ActivityController does not call Locomotion.proposal. Stationary
experiments also exclude WASD, CLICK_LOCAL_TARGET and INTERACT at the canonical
executor whitelist. A2 click cycling/clearance and anchor registration are not
armed. The existing HUD ROI selection remains available without anchor tracking.
Locomotion and existing recovery primitives remain available in the repository.
A fresh normal in-world observation begins STATIONARY_WAIT. Death, reset or
runtime/session loss is logged as RECOVERY_BLOCKER; no automatic return is attempted.

Gift flow preserves the existing detection/input/native ACK path, waits at least
ten seconds after the click (or first recovered received popup), then uses Use Later.
Clearance rejects both ACTIVE and gray PENDING HUD gifts and requires native
SetItemOpened_Complete HTTP 200 plus fresh world/UI clearance. Bounded ordinary
experiments re-arm the next gift without movement. Structured account/session
stationary events, movement_count and wait/gift intervals are fsynced in bounded
JSONL segments alongside the existing durable gift evidence.

Both live guests deployed isolated clean runtime snapshot
`4c7bdbea27753080f57ff249e526153a5c742f14` (initial snapshot
`f783676f7e09989c204bebc311585c6b6f118243`). The final one-line correction
lets stationary profiles reuse the existing production JOIN flow. The shared
checkout's pre-existing changes are retained; no main-checkout commit or push was
made. Canonical restart jobs 211 (A1) and 213 (A2) succeeded. Fresh GAME_READY and
new DST PIDs 1124 / 1160 were confirmed; old PIDs were 115983 / 62722.
Both prepared Cluster_1/cluster.ini files retain NETWORK idle_timeout=0 after
restart. Only that setting was changed, with cluster.ini.before-stationary backups.
This is configuration-after-restart evidence; no native idle-timeout getter is
exposed by the inspected Lua bundle.

User authorized ONLY a short 15–30-minute smoke and stopping afterward, not a
5–8-hour experiment. Final run `stationary_smoke_20261002T220856Z` passed:
A1 entered stationary wait at 22:10:53.118813 UTC; A2 at 22:11:31.103798 UTC.
Each completed at least 900 seconds (901.413 / 901.120 observed seconds).
Both stationary movement_count and legacy movement_commands stayed zero.
Each account had 111 distinct fresh world HUD samples; capture counts increased
by 2663 / 2670. Runtime stayed GAME_READY, screens stayed IN_WORLD_IDLE after
entry, with no worker restart or recovery blocker during the smoke. Start/end
screenshots show the living characters at the same station positions.
Both cluster.ini files were checked again after world entry: idle_timeout=0.
No gift appeared; the changed timed-gift reveal/clear cycle is therefore tested
by focused tests, not proven live in this smoke. This short run does not establish
the 5–8-hour gifting hypothesis or native effective idle-timeout getter value.

The smoke monitor disabled both workers automatically and finished at
22:27:37.341338 UTC. A fresh check at 22:37 UTC confirmed DISABLED on both,
held_inputs=false and driver_present=false, with runtimes still GAME_READY.
Schedules remain paused. No long experiment was launched.

Startup evidence is retained separately: a login gift transition initially timed
out; one existing-flow retry progressed. CV templates matched the fresh result
and were not changed. A1's final managed reload fell back to full runtime shutdown
because worker_healthy=False; it recovered GAME_READY before the final smoke.
A2 final reload preserved its game. This reload fallback remains a concrete
operational risk; no supervisor redesign was introduced.

Evidence and source provenance: ignored `.data/stationary-smoke/`; final report,
samples, start/end screenshots and copied guest stationary logs are under
`.data/stationary-smoke/stationary_smoke_20261002T220856Z/`.
Root checkout pre-existing changes were preserved; no main-checkout commit/push.

Final focused command (stationary, lifecycle, behavior, reward, deploy, runtime):
165 passed, one existing Starlette deprecation warning. Ruff on touched Python
files and git diff --check passed. New tests cover forbidden locomotion invocation,
canonical executor rejection with no driver events, ten-second reveal dwell,
operator precondition, recovery blocker, JOIN preservation and idle config edits.

## Gift modal latency correction — A2 deployed (2026-10-02)

A2 revision `08deb8604b131a9c41d695ae710a3dbc9d5eef3f`: Use Later timeout 45→6 seconds, one fresh strong hidden-modal/world postcondition; high-confidence received-title/button may act on the first valid frame. UNKNOWN retains local perception for up to eight frames before broad fallback. Deadline-poll timeout now allows the existing second close attempt instead of prematurely latching intervention. Native ACK + fresh active-icon disappearance remains mandatory. A2 second gift confirmed in durable DB at 17:57:00.226184 UTC before managed code reload; accelerated flow itself awaits a subsequent live gift. A1 deployment unchanged.

## A2 click-cycle drift correction (2026-10-02)

A2 remains operator-stopped. Deployed revision `51c50186dd752f9aa7dacf750943f9873d62ac78` removes blind relative cycling on registration loss. Click targets remain absolute offsets from the original reference, not the last commanded point; arrival requires fresh measured proximity within 2 px. Failed legs return to center before another target; blocked targets cool down. Outside ±32/±20 px, only inward center correction is allowed. No live movement validation was performed because A2 is explicitly stopped. The older relative-cycle override below is superseded.

## Current operational override — account 2 click cycle (2026-10-02)

User explicitly replaced A2's camera-registration hold with a simple small click
cycle, without test runs: RIGHT +8 px, UP −6 px, DOWN +6 px, LEFT −8 px to center.
A2 avatar was located beside the right post of the arch, feet near (640,400) in the
canonical 1280x720 GAME viewport. A short rightward key pulse clears the arch's
Examine hit area once; then existing canonical click/input/action/ACK paths send
CLICK_LOCAL_TARGET. Camera registration uses one static arch detail to compensate
world-to-screen targets; a closed relative cycle continues if that reference drops.
No blind displacement is credited as verified movement. Local click timeout advances
the cycle rather than latching intervention. Fresh ACTIVE >=.94 preempts clicks;
existing native ACK + fresh active-icon disappearance collection proof remains.

A1 keeps its original movement deployment; it was resumed from a benign gift-hover
timeout. The run monitor uses canonical worker/resume for that specific timeout.
A2 revision: `cab64cd537c7c43a9934281dc1204ddb76d19ffb`; existing DST PID preserved.
Current run ID and planned end remain unchanged below; account-specific policy
changes and interrupted periods must be included in the eventual report, not hidden.
Guest A2 local-click-anchor.json and local-click-reference.png are runtime artifacts;
route diagram is ignored `.data/local-gift-zone/click-route.svg`.

## Five-hour observation run — RUNNING (2026-10-02)

`gift_zone_run_20261002T153220Z` uses the SAME bounded local policy on accounts 1/2.
Existing DST clients remain PID 115983 / 62722; no new accounts or A/B.
Minimal live-audit fixes: 14 small anchor-patch candidates with a uniquely dominant
consensus (including static arch details), cooldown-only obstacles INCLUDING center,
short key pulses timed inside the canonical xpra bridge, native receipt DB acceptance,
and lightweight state/summary logs. Runtime revision:
`439efe78e8d7bff563da360c8f0b9a00dc9fad76` on both guests via managed adoption.

Both fresh GAME_READY workers actually sent and verified local movement before the
clock began: `2026-10-02T15:33:15.613640+00:00`; planned end:
`2026-10-02T20:33:15.613640+00:00`. Historical collected UNKNOWN; run counters zero
at start. Earlier interrupted startup/audit windows are separate and not counted.
Live evidence isolated two corrections during audit: ACK latency prolonged held keys
and exceeded the soft boundary before inward correction; A2 had too few textured
patches after one reference degraded. Both are fixed in this deployed revision.

Autonomous monitor/end/report unit: `dst-gift-zone-run-20261002T153220Z.service`
(Restart=on-failure); gameplay remains in existing supervised guest GameWorkers.
Source: `scripts/gift_zone_run.py`. Metadata, incremental guest events, minute
telemetry, alerts and eventual final-report.json/.md: ignored
`.data/gift-zone-runs/gift_zone_run_20261002T153220Z/`.
Guest evidence/events remain in `/home/dst/.local/state/dst-runtime/experiments/`
under run `_a1` / `_a2` sessions. Stop only at planned end or real anchor/session
loss; ordinary obstacle failures continue inside the same local zone.
Correlation in this run cannot establish GREY eligibility or activation causality.

## Active checkpoint — local gift-zone worker (2026-10-02)

Current user-authorized task replaces the A/B movement policy with the same local
operational behavior for both existing accounts. The operator manually places each
character in the activation-zone center before takeover. No A/B comparison, world
route search is authorized. The current follow-up explicitly authorizes a five-hour
observation run on these same two accounts, with historical_collected UNKNOWN and
run-scoped confirmed counters starting at zero.

Implementation: canonical bounded key input is retained; the current movement path
has no calibrated movement-click primitive. The first takeover viewport supplies
14 small static-world patch candidates, registered through the existing perception engine.
At least two patches must agree in a uniquely dominant cluster. Targets are offsets from that anchor in canonical
1280x720 viewport pixels: left/right 18, up/down 10, diagonals (±12, ±8), with center
returns and an inward correction boundary of (±32, ±20). Pulses are 0.025–0.10 seconds;
these pixel limits are deliberately conservative, not a proven 3–5 world-unit mapping.
Short movement pulses now send key-down/up in one existing xpra bridge operation,
with the ACK after release, preventing an ACK round trip from extending the hold.
A fresh directional world translation of at least two pixels verifies movement;
after three seconds without displacement, the target is withheld for four selection
cycles and another nearby target is selected. Even eight consecutive unverified targets remain temporary cooldowns; fresh
registration is required for every retry. Missing anchor registration holds input.
The anchor survives resource recovery; takeover explicitly resets it.

One fresh verified ACTIVE gift at confidence ≥0.94 latches immediately, including
while movement verification is pending, and preempts movement for the existing gift
flow. Gray/pending gifts do not latch. Local-mode observation/tick delays are 0.1
seconds; the existing detector uses only world/gift/safety ROIs while the world is
known, with full classification restored after an unrecognized state. This is a
sampling interval, not proof that every rendered frame is captured.

Managed success now requires a fresh world observation without an active gift icon
and this account's native SetItemOpened_Complete HTTP 200 after its canonical click.
Received/Use Later closing still uses the existing verified flow. The same durable
receipt, state/DB heartbeat lifecycle, and next-gift rearming remain in use.

Deployment uses an isolated clean source snapshot because the shared checkout had
pre-existing uncommitted edits; no main-branch commit or push was made. Final runtime
snapshot revision: `7514f056e24eb8f8ae6ffe98423d8a9b27c953c5`. Provenance and a prepared
canonical activation command are in ignored `.data/local-gift-zone/`. Authenticated
clients are preserved by the existing managed-process adoption deployment path.
Both final deployments completed with unchanged managed-process identities; fresh
accepted GAME_READY heartbeats and RUNNING/GAME_READY account status were observed
for both guests. Both canonical workers remain DISABLED.

**LIVE_PROVEN gift collection on both accounts (2026-10-02 14:25–14:31 UTC):**
The operator explicitly requested claiming A1, then A2. Each canonical worker clicked
its fresh ACTIVE gift once, then continued the existing received/Use Later flow.
The initial short wall budget expired during slow gift opening; continuation reused
the same session/evidence and did not re-click the gift icon. Both returned to fresh
IN_WORLD_IDLE frames with the active gift icon absent and durable
IN_WORLD_GIFT_CONFIRMED / CLAIM_CONFIRMED receipts. Both workers ended DISABLED.

- A1: Loafers, item `961697195743394926`; native ACK HTTP 200 / Error=false at
  14:27:03.403108 UTC, fresh world postcondition at 14:30:16.426384 UTC.
- A2: Arctic Explorer’s Mitts, item `745510462931798848`; native ACK HTTP 200 /
  Error=false at 14:28:09.857565 UTC, fresh world postcondition at
  14:30:52.531209 UTC.
- Evidence: ignored `.data/local-gift-zone/finish-a1.json`, `finish-a2.json`,
  `a1-complete.png`, `a2-complete.png`; guest durable receipts are under the
  matching `local-claim-a1-20261002T142525Z` and `local-claim-a2-20261002T142602Z`
  experiment folders. The command-schema name does not denote an A/B run.
- Previously observed persistence limitation: Control Plane only accepted recorded
  received-frame receipts. A minimal fix now also accepts authoritative
  NATIVE_ACK_AFTER_CANONICAL_CLICK with ordered native ACK and fresh icon-disappearance
  postcondition. Twelve focused persistence tests passed and the Control Plane was
  reloaded; the new-run path will use this ingestion.
  Do not confuse this DB limitation with an unclaimed gift or repeat the claim.

Permanent local movement has not been live validated or left running; this request
was fulfilled as collection with stop after confirmation.
Focused offline action/perception/behavior checks passed after resolving the changed
first-frame expectations; the final gift-evidence/transition suite passed 45 tests,
local movement plus native-postcondition check passed 11 tests. First-frame latch
and anchor translation direction were checked offline. Ruff and diff whitespace
checks passed.

## Historical checkpoint — resumed dual workers (2026-10-02)

Current user-authorized goal: keep A1 CONTROL and A2 HIGH_ACTIVITY running toward
14 valid in-world hours per account. The user explicitly authorized fixing and
restarting both workers. The preparation-only launch restriction below is historical.

Deployed runtime revision `a40af6eeb5f1711d49e3a9fc65c5f3f94f402863` to both guests
through managed-process adoption. Steam, DST and display PIDs remained unchanged.
Canonical SET_MODE commands 902 (A1) and 903 (A2) returned ACK OK.

- A1 session `a1-control-recovered-20261002T013437Z`: CONTROL; prior valid
  3180.748 seconds credited; remaining target 47219.252 seconds.
- A2 session `a2-high_activity-recovered-20261002T013448Z`: HIGH_ACTIVITY; prior
  valid 3117.908 seconds credited; remaining target 47282.092 seconds.
- Original wall deadlines remain A1 2026-10-02 15:53:51 UTC and A2 15:45:19 UTC.
  Invalid world time does not advance the valid-time target; the wall deadline can
  stop a run before that target if too much invalid time accumulates.
- Automatic schedules remain paused. VIEW_ONLY does not require worker disable.

Fixed movement hold timing to begin after key-down acknowledgment, preserved
cancellation status when ownership is revoked, and allowed subsequent actions after
an acknowledged bounded timeout with no held or uncertain input. Concurrent PAUSE
still keeps the input gate closed. Unavailable gift transitions now defer within the
existing retry policy rather than permanently stopping normal idle-world activity.

**LIVE_PROVEN, bounded:** a 180-second monitor observed both workers remain ACTIVE
and execute perception-verified movements. A1 initially had two failed movement
checks, then verified movement and cleared the consecutive-failure counter. At
01:38:16 UTC A1 had 4 movement commands / 137.042 valid seconds, A2 had 12 commands /
159.442 valid seconds; both counters for consecutive movement failures were zero.
This does not prove completion or unattended reliability over the full 14 hours.
Both gifts remain PENDING_GIFT_UNCLAIMED; no new gift receipt was confirmed.
Evidence: ignored `.data/worker-fix-20261002/` (before, launch ACKs, PID identities,
monitor and screenshots). Focused action/behavior/locomotion tests: 107 passed;
shifted-gift tests: 4 passed; Ruff and diff whitespace checks passed.

## Historical checkpoint — dual A/B preparation (2026-10-01)

Current user-authorized goal: prepare two isolated online DST sessions for a bounded
CONTROL / HIGH_ACTIVITY locomotion smoke and a future weekly-gift experiment.
Do not launch the multi-hour experiment during preparation. Do not repeat completed
storage, provisioning, authentication, single-client measurement or input-isolation
work without regression evidence.

**LIVE_PROVEN: READY_FOR_DUAL_AB.** Final concurrent 120-second profile run:
`.data/dual-ab/20261001T041729Z-a12d5bd0/`; final 130-second health monitor:
`.data/dual-ab-preparation/dual-health-f5fbcba.jsonl`. Both independent sessions
stayed ONLINE and `GAME_READY` in all 54 samples per account, with no Steam/DST
process or worker restarts. Maximum heartbeat gap was 10.6 sec. Both returned to
`DISABLED` and `IN_WORLD_IDLE` when the bounded run ended. The only recovery-failure
counter was two on A1; actual recoveries/reconnects and movement failures were zero,
so there was no recovery loop. Each worker had one perception timeout, with no
perception failure total or persistent loss of GAME_READY.

- A1: runtime 1 `dst-000001-g1`, DISPLAY `:99`, session
  `a1-r1-7ce9f5c5fd524530bc20f2dfde729fb3`, CONTROL. 3 commands / 82.743 valid
  world seconds = 2.175 commands/min; 0.8 moving seconds = 0.580 moving sec/min.
  Current gift HUD is gray `IN_WORLD_GIFT_PENDING`; item service healthy with one
  pending item. Do not claim it unless fresh authoritative state marks it claimable.
  Latest durable confirmed claim T0_A1: `2026-10-01T00:18:27.798283Z`.
- A2: runtime 4 `dst-000002-g2`, DISPLAY `:100`, session
  `a2-r4-b1fc6e3c91f54109a40f1f1d5da3d19b`, HIGH_ACTIVITY. 13 commands / 90.817
  valid world seconds = 8.586 commands/min; 19.2 moving seconds = 12.685 moving
  sec/min (21.9x CONTROL movement time). Item service healthy, cache has zero
  pending items, in-world gift visual is `GIFT_AVAILABILITY_UNKNOWN`. Its actual
  claim is complete and must not be repeated: item `399365579731404687`, native
  `SetItemOpened_Complete` HTTP 200 at T0_A2
  `2026-10-01T02:57:55.223013Z`. Durable recovery receipt:
  `.data/dual-ab-preparation/account2-inworld-receipt.json`.
- Reciprocal canonical GameWorker input-isolation test LIVE_PASS: one bounded
  movement command per account changed only its own display; the other display
  stayed unchanged. Evidence/screens are in ignored `.data/dual-ab-preparation/isolation/`.
- Per-account session IDs, guest evidence folders, screenshots, telemetry streams,
  item-service state and receipts are separate. Gift detection/claim completion
  paths operate independently per account; a completed gift for one does not finish
  the other. No gift was claimed during the final profile smoke.

Bounded dual capacity sample (`.data/dual-ab-preparation/dual-capacity-f5fbcba.json`):
host 11.30 GiB used / 4.26 GiB available, aggregate CPU 77.7%, load 16.82 / 15.75 /
15.48, CPU PSI some avg10 87.25% / full 0%, memory PSI some 0.41% / full 0.21%,
swap 67.5 MiB with no swap I/O during the sample. A1 used 6.24 GiB cgroup RAM and
1.50 CPU cores; A2 5.78 GiB and 1.56 cores. No OOM count changed, no swap thrashing,
no client stall/restart, and both locomotion profiles worked. CPU PSI is elevated but
was not a functional capacity blocker in this live run.

Guest storage resize was already confirmed after the Azure change: 128 GiB block
device, about 123 GiB ext4 root, approximately 56 GiB free; Incus `default` pool
available. Storage work is complete; do not audit again in this stage.

Both automatic account schedules are durably paused so LONG_SESSION jobs cannot
compete for experiment input. The canonical launcher pauses them idempotently and
waits for existing work to drain. This preserves authenticated clients and leaves
scheduling opt-in. Future explicit full-run entrypoint (not run during preparation):

```sh
.venv/bin/python scripts/dual_ab.py --env-file .data/linux-validation.env --until-gift
```

It creates fresh separate session/evidence IDs, runs Account 1 CONTROL and Account 2
HIGH_ACTIVITY, monitors each gift independently, and requires explicit `--until-gift`;
its normal default is only a bounded smoke. The earlier Account 2 daily receipt
`2026-10-01T01:46:26.925098Z` remains durable separately from its later in-world
claim. Account 3 runtime `dst-000003-g1` was not assigned or changed.

Storage and account prerequisites, individual account health, both profiles, gift
receipts, and reciprocal input isolation are LIVE_PROVEN. Relevant code checks passed
before deployment: focused scheduler/API/launcher/worker tests, Xpra input tests (15),
Ruff, and `git diff --check`. Recent commits include scheduler reservation, robust
inventory-border perception, failure diagnostics, and bounded Xpra handshake timeout.
No multi-hour experiment, screenshots, credentials, logs or temporary evidence were
committed. The current source branch is `main`; changes are ready to push.

## Current business goal

**One-account target:**

enter account/world

-> handle actionable gift

-> perform only required playtime/activity

-> handle next gift

-> repeat safely

## Live-proven — do not revalidate by default

The following paths/capabilities have already been proven against the live runtime:

- Control Plane -> Runtime Agent -> GameWorker -> canonical input -> DST -> fresh
  verification -> ACK/result.
- `MAIN_MENU -> Host Game -> Farm 01 -> Survivor Select -> Wilson/Wardrobe -> IN_WORLD_IDLE`.
- Host Game hover/focus recovery.
- Selected/hovered survivor portrait handling.
- Bounded movement with gameplay-region verification.
- Canonical `PAUSE_WORLD` and `RESUME_WORLD`.
- Death/reset recognition.
- Xpra canonical input path and bounded reconnect handling.
- Runtime Agent SIGHUP token/adoption handoff.
- Steam/Klei persistence through managed recovery.
- Control Plane `STALE -> RUNNING` recovery after a healthy authenticated `GAME_READY`
  heartbeat while the configured GameWorker remains `DISABLED`.

Do not rerun these acceptance paths merely because a new task starts. Revalidate a path
only when code directly affecting it changed or new evidence indicates regression.

## Current milestone

**FIRST REAL IN-WORLD GIFT CLAIM — LIVE_PROVEN (2026-09-30)**

- Existing session retained: production GameWorker detected ACTIVE, used canonical
  `CLICK_GIFT_ICON`, observed `Opening` / `You Received`, and clicked `Use Later`.
- Received Pinstripe Pants, item `986745024922813965`. Native item service reported
  `SetItemOpened_Complete Success:200`, `Error=false`, Modified `1790780509.1803596`.
- The initial close-verification timed out. Verification-only recovery used the actual
  canonical action recording plus two fresh world observations; no input was replayed.
  Worker generation 1006 persisted `IN_WORLD_GIFT_CONFIRMED` atomically with fsync.
- Evidence: `.data/claim-convergence-20260930/` (local, excluded from git), guest
  `/home/dst/.local/state/dst-runtime/claim-convergence/confirmed/`.
- Path B used the existing production GameWorker through a bounded one-shot adapter.
  Systemd `KillMode=control-group` includes Xvfb/Steam/DST, so no restart was attempted.
  Agent PID 491 remains SIGSTOP suspended to prevent competing input/lifecycle cleanup.
  Xvfb 498, Steam 507/600, DST 1026/1237 remained unchanged. Worker stopped cleanly,
  held inputs false; Control Plane heartbeat remains STALE.
- Durable result is native backend ACK + fsynced worker state, not a Control Plane DB
  claim or weekly counter. Normal managed worker receipt-provider wiring is not proven.
- In-world receipt semantics are distinct from daily login. Daily claim, repeat gift,
  weekly ordinal/target/reset, eligible timing, and AFK/activity requirement remain unknown.

## Current environment direction

Farm 01 should become a durable prepared single-account world. Prefer a persistent safe
world, a vanilla Science Machine near the operating position, and minimal required
gameplay. Avoid building navigation/crafting/survival AI merely to recreate
infrastructure that can persist in the world.

LIVE_PROVEN on 2026-09-29: Farm 01 was rebuilt after the previous prepared save was
replaced by a DST world reset. The latest restored Master save contains exactly one
vanilla `researchlab` at `(374, 196)`; its live provisioning GUID was `128894`.
After restoring the prepared snapshot, a fresh canonical 1280x720 `IN_WORLD_IDLE`
frame showed alive Wilson and the machine beside the Florid Postern. GameWorker was
returned to DISABLED. A further clean managed STOP/START again loaded the same world;
a bounded canonical rightward input visibly moved Wilson while the machine remained
in view. `Master/modoverrides.lua` is empty; neither temporary fixture
mod is enabled. The rollback point is Incus snapshot
`dst-000001-g1/PREPARED_ALIVE_PRE_DEATH_20260929_1315`, also preserved as
`~/.local/state/dst-backups/Farm_01_PREPARED_ALIVE_PRE_DEATH_20260929_1315_save.tar.gz`.
Future gift work must reuse this machine and must not provision or build another one.

The controlled death validation observed alive, death, reset countdown, and a new
post-reset Master session with zero Science Machines. Its compact evidence archive is
`~/.local/state/dst-backups/Farm_01_RESET_OCCURRED_20260929_1333_evidence.tar.gz`.
The prepared snapshot was then restored through the managed STOP/START lifecycle.

## Stage 1 — verified daily gift claim

The worker now reports an explicit `DAILY_GIFT_CONFIRMED` result only when a verified
`CLICK_REWARD_OPEN` action is followed by a fresh `REWARD_RESULT` observation containing
the verified “You Received” title and close anchor. `GIFT_AVAILABLE`,
`GIFT_INTERACTION_STARTED`, `GIFT_UI_OPEN`, and `GIFT_UI_CLOSED` remain separate states.
The bounded worker telemetry includes the confirmation's evidence frame, sequence, and
action ID. Fixture-backed tests cover the positive result, available/banner-only,
open-in-progress, dismissal, and duplicate-result cases. This offline proof covers the
recorded daily login reward flow; it does not prove an in-world Science Machine gift
claim. No reward modal is currently available for live validation.

Preflight on 2026-09-29 found a launch configuration mismatch: Control Plane
`RUNTIME_AUTO_LAUNCH_STEAM=false` and `RUNTIME_AUTO_LAUNCH_DST=false`, while guest
Runtime Agent `AUTO_LAUNCH_STEAM=1` and `AUTO_LAUNCH_DST=1`. These settings were left
unchanged; no lifecycle operation was performed. The guest worker configuration is
`WORKER_MODE=DISABLED`, `WORKER_AUTOSTART=0`. A fresh read-only 1280x720 X11 capture showed
the game at Survivor Select, with chat reporting that `sociopath` was killed by
Darkness; no player was verified alive in-world and no gameplay input was sent.
Rollback remains available at Incus snapshot
`dst-000001-g1/PREPARED_ALIVE_PRE_DEATH_20260929_1315` and host archive
`~/.local/state/dst-backups/Farm_01_PREPARED_ALIVE_PRE_DEATH_20260929_1315_save.tar.gz`
(1,122,046 bytes, present at preflight).

## Stage 2 — durable gameplay task and reward persistence

`DAILY_GIFT_CONFIRMED` now enters the existing Control Plane runtime-heartbeat
transaction and is stored as a durable `GameplayTask` result, linked to the owning
account, runtime, and current WorkerRun. Available, interaction-started, UI-open,
UI-closed, and confirmed results remain distinct; only a structurally valid
`DAILY_GIFT_CONFIRMED` payload can complete a task. Confirmation identity is scoped to
account and task kind and derived from the worker action ID plus evidence frame ID;
it does not assume a calendar-day reward cycle. Database uniqueness protects against
duplicate delivery, and the same transaction writes the successful task and claim.

Migration `0007_gameplay_task_results` adds `gameplay_tasks`, its account/runtime/
WorkerRun links, success/status constraints, and active-task/claim uniqueness. At the
end of Stage 2, the populated validation database remained at
`0006_worker_remote_view`; a temporary backup clone successfully upgraded to 0007,
downgraded to 0006, and upgraded again. No PostgreSQL integration database was
configured for that validation.

Stage 2 persistence is implemented and offline-verified. Stage 1 real live gift
confirmation remains `STAGE_1_LIVE_VALIDATION_PENDING`; no claimable gift was consumed
or fabricated. Runtime/player state was not freshly inspected during Stage 2, and no
live gameplay or lifecycle action was performed. The prepared rollback snapshot and
archive recorded above were not modified by this stage; their presence was not
rechecked during Stage 2.

## Stage 3 — single account daily task executor

The deployed Control Plane service uses `.data/linux-validation.env`; its active
database is `.data/linux_validation.sqlite3`. It is now migrated through
`0010_long_session` (including the gameplay result migrations). Production PostgreSQL
migration remains untested.

For managed runtime startup, Control Plane `RUNTIME_AUTO_LAUNCH_STEAM` and
`RUNTIME_AUTO_LAUNCH_DST` in `.data/linux-validation.env` are authoritative. Runtime
Bootstrap renders those settings into the guest Runtime Agent environment on managed
start/rebuild; the guest copy is generated configuration. Bootstrap version 2 forces
the next start to apply the reconciled configuration. Both settings are true for the
normal managed launch path.

The existing scheduler invokes the durable `DAILY_GIFT_CLAIM` job. The executor
reconciles STOPPED/STARTING/RUNNING runtime states, requests fresh GameWorker
OBSERVE perception after `GAME_READY`, then uses the existing worker command path.
Only Stage 2's persisted `DAILY_GIFT_CONFIRMED` result completes a task. Normal
no-reward is represented as `NO_REWARD_AVAILABLE`; other unsafe or unreconciled
conditions remain inspectable `NEEDS_ATTENTION` results. Worker disable/input release
and owned-runtime stop run through cleanup paths.

Focused Stage 1, Stage 2, runtime/job, migration, and Stage 3 tests passed after the
report-shape and SQLite UTC timestamp checks were added. The synthetic lifecycle
covers managed STOPPED -> STARTING -> GAME_READY -> fresh observation -> ACTIVE ->
confirmed result -> worker disabled -> managed STOPPED. PostgreSQL migration is not
covered here.

Bounded live validation used a fresh OBSERVE-only worker command. It produced
`CHARACTER_SELECTION` at 0.979 confidence. The screen was not accepted as a safe
initial task state because the prior fresh capture contained the death-by-Darkness
chat message. No ACTIVE command or gameplay input was sent. Worker mode ended
`DISABLED`, held inputs false; runtime remained in its pre-existing running state.
There was no actionable reward evidence, no `DAILY_GIFT_CONFIRMED`, and Stage 1 live
claim validation remains pending. Rollback resources remain preserved.

## Current blockers

- The production fixed-coordinate path
  `MAIN_MENU -> IN_WORLD_IDLE` is LIVE_PROVEN on deployed revision
  `67e957f3cd939eb48cb2c7bc60243cb106a7d6ad`. A fresh stable `MAIN_MENU` at
  2026-09-29 09:34:33 UTC reached worker-verified `IN_WORLD_IDLE` at 09:37:03.9 UTC
  (2m30.9s); two independent fresh `IN_WORLD_IDLE` observations followed at
  09:37:45.5 and 09:37:46.6 UTC. No Control Plane VERIFY or worker reactivation was
  required during entry. No action timeout occurred, so local late-transition recovery
  was not naturally exercised. Per-state stable-state-to-action timings were not
  retained in the Control Plane status history. The largest adjacent action-status
  interval was 47.3s, including an unclassified game transition; its non-Loading idle
  portion cannot be isolated.
- Fixed menu click locations must continue to come from
  `dst-1280x720-linux-v1`; do not return to dynamic anchor localization for these
  controls unless the supported display profile changes.
- The first actionable gift frame has been captured; its exact activation time is unknown.
- Stage 1 live status remains `STAGE_1_LIVE_VALIDATION_PENDING`: a live actionable
  reward must still be claimed and confirmed.
- Safe world profile ownership and current-process configuration evidence are now
  implemented and passed a 10-minute live session on 2026-09-29. The short run proves
  managed world entry, fresh config/process evidence, 51 in-world checkpoints, and
  clean stop; it does not prove the settings' gameplay effects. Do not start the
  75–90 minute soak until the profile's effect on the existing world is separately
  established.
- Active Control Plane SQLite schema is `0010_long_session`; PostgreSQL migration
  has not been tested.
- The in-world path still needs recorded evidence of (1) an enabled present beside the
  Science Machine, (2) the post-`INTERACT` reward UI showing `You Received` and
  `Use Later`, and (3) a fresh `IN_WORLD` frame after `Use Later`. Existing reward fixtures
  are from the daily login modal and cannot prove that in-world path.

## Completed worker semantics

- An ACTIVE GameWorker performs deterministic world entry with
  `validation_flow=false`; the legacy validation route remains test-only.
- Reusable gameplay primitives are independent of validation-flow orchestration; the
  validation flag enables only the legacy one-shot test route.
- Recoverable worker intervention no longer rewrites configured ACTIVE intent to
  DISABLED. Unsafe states suppress action execution until observation or recovery is
  verified.
- Mods Disabled confirmation is supported by fixed production UI handling; the
  deployed confirmation action cleared the live modal and its fresh verification
  observed the transition.
- The canonical host-to-guest application deployment path is implemented and
  LIVE_PROVEN; the guest reports revision
  `f723a9ee5090529240e3775d68a426abfa2a6aff`. The post-recovery-change production
  world-entry acceptance completed; GameWorker was returned to DISABLED afterward.

The gift milestone is not complete from the gray-present observation alone. An enabled
present has since been captured. The real claim sequence and fresh-state verification
remain outstanding.

## Stage 3B safe world-entry reconciliation (2026-09-29)

GameWorker now classifies a survivor-selection screen from verified title, player-list,
and Wilson-target anchors, and uses the observed Wilson portrait / loadout button anchors
for canonical selection actions. Action verification still requires fresh observations;
loading is intermediate and only stable fresh `IN_WORLD_IDLE` completes entry. The daily
executor admits character selection only when those structural anchors are verified.
Partial screens, DEAD, reset-pending, and unknown states remain non-actionable.

The bounded live preflight found the runtime already RUNNING and worker DISABLED with no
held inputs. A fresh screen was the normal Survivor Select UI, with an on-screen death
message. Read-only save inspection found the active Master save on a different session
from `PREPARED_ALIVE_PRE_DEATH_20260929_1315`; the prepared archive contains the expected
Science Machine fixture and the active save does not. The rollback snapshot/archive still
exist and were not restored or consumed. No survivor selection, world start, reward action,
or new daily task was attempted. The pre-existing `NEEDS_ATTENTION` task remains durable;
the worker remains disabled and the inherited runtime remains running.

Stage 3B focused regression command passed 158 tests across daily executor, gameplay
persistence, perception/actions/transitions, Stage 1 behavior, runtime/jobs, and
migrations. Ruff lint and `git diff --check` pass. Ruff format check identifies existing
format drift in touched historical files; those files were not wholesale reformatted to
avoid unrelated churn. Live world-entry/claim validation remains blocked until the
prepared world is safely available again; no automatic rollback was performed.

## Stage 3C controlled prepared-world restore (2026-09-29)

The current post-death container was preserved as Incus snapshot
`STAGE3C_CURRENT_POST_DEATH_20260929_1847`, with a separate compact save archive
`Farm_01_STAGE3C_CURRENT_POST_DEATH_20260929_1847_save.tar.gz`. Its active Master
session was `AC6D72B5B368FA58` with no Science Machine record. The prepared snapshot
`PREPARED_ALIVE_PRE_DEATH_20260929_1315` and its independent archive remained intact.
After managed STOP and worker/input quiescence, the existing runbook snapshot restore
passed the read-only pre-death guard: Master session `EA6E12E4296C650B`, save
`0000000003`, one `researchlab` at `(374, 196)`, and empty mod overrides.

Managed START restored process readiness. Bootstrap version 3 now retains the verified
`dst-1280x720-linux-v1` worker calibration profile on managed starts. The canonical
runtime deployment restored Stage 3B worker code after the older container snapshot
reverted it. Fresh GameWorker observations followed canonical world-entry actions from
`MAIN_MENU` through the saved-world list and Loading to valid `IN_WORLD_IDLE` with a
visible player. The same prepared save/fixture remained on disk after entry and after
the daily task. The worker ended DISABLED with no held inputs; the inherited runtime
remains RUNNING and healthy.

The previous GameplayTask 1 remains `NEEDS_ATTENTION / WORKER_OBSERVATION_UNSAFE`.
Migration 0009 treats that state as a terminal attempt, allowing a distinct task 2.
The production DAILY_GIFT_CLAIM job 70 ran through the executor and persisted task 2
as `NEEDS_ATTENTION / NO_CLAIMABLE_REWARD_UNVERIFIED`: the worker observed a stable
in-world state but its gift telemetry stayed `UNKNOWN`, so reward unavailability was
not asserted. No `DAILY_GIFT_CONFIRMED` occurred; Stage 1 live validation remains
pending. Stage 3C restore and live world entry are proven; the next gift work needs a
real actionable reward or a verified unavailable-reward signal.

## Stage 3D — direct HUD gift availability (2026-09-29)

Status: `PASS_ACTIVE_SAMPLE_PENDING`. The user-verified top-left gift HUD icon's
gray/inactive state now supplies a production unavailable-reward signal. This
supersedes the previous absence of verified unavailability evidence.

`VisionDetector` reuses `world_present_banner` within calibrated normalized ROI
`(0.115, 0, 0.2, 0.14)` for `dst-1280x720-linux-v1`. On fresh valid in-world frames,
template identity plus masked icon chroma establishes `NO_REWARD_AVAILABLE`.
Missing/ambiguous/stale evidence stays `GIFT_AVAILABILITY_UNKNOWN`. One canonical
hover is available for uncertain identity; its localized UI response is secondary
evidence, never gift availability or claim confirmation. Hover transport/response
are covered by focused tests; hover was unnecessary and was not exercised live.
No trustworthy active/colored reference exists: `ACTIVE_ICON_LIVE_SAMPLE_PENDING`.
The active branch requires an independently verified reference and remains unproven.

The runtime advanced to a real death countdown during implementation, before any
Stage 3D gameplay input. A full current-state snapshot failed with ENOSPC and Incus
removed its partial copy. Managed STOP job 71 failed while snapshotting held Incus
in FROZEN state. The current saves were then preserved in verified compact archive
`Farm_01_STAGE3D_CURRENT_PRE_RESTORE_20260929_1941_save.tar.gz` (SHA-256
`7c5602fbef92848678f17ecf4284fd9fe12794f1b10cf83aac5dc63aad984297`).
Managed STOP job 72 succeeded; the existing runbook restored the untouched
`PREPARED_ALIVE_PRE_DEATH_20260929_1315` snapshot once while STOPPED. The pre-death
guard independently verified matching save bytes, one machine at `(374, 196)`,
and disabled temporary mods. Managed START job 73 and canonical runtime deployment
revision `f48c31f16b8ff24b7a4076d192b8a7684a29ed9e` restored healthy GAME_READY.
No automatic recovery policy was added.

Fresh canonical world entry reached living `IN_WORLD_IDLE`; the live icon bounds
were `(168, 10, 226, 68)`, identity confidence `0.9999367`, chroma p95 `13`, and
colored fraction `0`. Production DAILY_GIFT_CLAIM job 74 created distinct task 3
and durably persisted `NO_REWARD_AVAILABLE` with frame `r1-w1-f2` observed at
`2026-09-29T19:47:14.548776+00:00`. Tasks 1 and 2 retain their original
NEEDS_ATTENTION history. No DAILY_GIFT_CONFIRMED occurred; Stage 1 live validation
remains pending. The combined focused Stage 1/2/3/3B/detector suite passed 155 tests.

Final managed STOP job 75 succeeded after task cleanup, preserving the alive baseline
instead of leaving it running unattended. Runtime 1 / `dst-000001-g1` is STOPPED;
worker mode DISABLED, process state STOPPED, held_inputs=false. Final read-only save
verification still finds Master `EA6E12E4296C650B`, save `0000000003`, exactly one
Science Machine at `(374, 196)`. Both original rollback snapshots and their archives,
plus the Stage 3D compact current-state archive, remain available. Stage 4 was not run.

## Stage 4A — unattended-session preflight (2026-09-29)

Historical preflight; Stage 4A.1 below records the later session and the implemented
long-session task and storage guard. The safe-profile concern remains current.

Status: `BLOCKED / NOT_READY_FOR_LONG_SOAK`. No runtime start, gameplay input,
snapshot restore, world setting change, new session task, or live soak was performed.
The active/colored gift sample is not this blocker.

Read-only verification still finds prepared Master `EA6E12E4296C650B`, save
`0000000003`, exactly one Science Machine at `(374, 196)`, and no temporary mods.
The installed legitimate world settings expose nonlethal darkness, hunger, and
temperature damage. The prepared world's `leveldataoverride.lua` still uses
`darkness="default"`, `hunger="default"`, `temperaturedamage="default"`, default
hostile/environmental threats, and normal day/night progression.

Reward-safe environmental simplification is NOT established. The installed
`scripts/components/giftreceiver.lua` obtains gift count through native
`TheInventory:GetClientGiftCount`; it does not expose playtime eligibility or prove
that changing world settings preserves that eligibility. No project evidence or
authoritative documentation inspected establishes that exact requirement. The
availability of legitimate nonlethal settings alone is insufficient proof.

The alternative minimal survival policy also lacks a usable night-light capability:
prepared player saves contain none of torch, lantern, minerhat, or lighter. The world
has no campfire/firepit/lantern/nightlight record. Its sole torch record is at
`(-149.54, -277.61)` with fuel `8`, far from the prepared operating position, and
does not establish a sustained light source. Existing canonical actions and
navigation/recovery primitives do not provide verified light acquisition,
equipping, replenishment, or crafting. Pausing cannot substitute for a proven
unpaused reward session. Starting an idle soak would repeat the known Darkness risk.
No speculative survival economy or unused session framework was added.

At that preflight, storage was approximately 6.6 GiB free; Incus uses the `dir` driver on the same
host filesystem. Live Klei data is 14 MiB; host journals are 203 MiB and guest journals
461 MiB. Recording is DISABLED with zero frames/events; existing per-recording defaults
are 15 minutes, 1800 frames, 512 MiB, queue size 8. No explicit journald size override
was found on the host. All three compact save archives passed gzip integrity checks;
the prepared and Stage 3C rollback snapshots were retained. No large snapshot was
created. A future session must enforce a minimum 2 GiB free-space guard on the host/
Incus filesystem and bound aggregate diagnostics/log retention; those guards were
not yet implemented at that preflight.

At that preflight, runtime 1 was STOPPED and the worker DISABLED. Stage 4A.1 below
records the later session and current blocker. No 6–10 hour soak was started.

## Stage 4A.1 — safe profile and first unattended soak (2026-09-29)

Status: `BLOCKED before the 75-minute soak`. The long-session task, storage guard,
and bounded monitoring are implemented, and the first production session completed.
However, the safe built-in profile did not persist through actual world entry, so the
run cannot validate survival under that profile and the second soak must wait.

The original prepared config was backed up at
`~/.local/state/dst-backups/STAGE4A1_original_config_20260929.tar.gz`; its original
Master `leveldataoverride.lua` SHA-256 is
`47057a5fbad74a41fc7c863c511093bf63ad6d78fa4399689430482399726944`. The managed
bootstrap applied the experimental profile before launch, but after the game entered
the world the file hash returned to that exact original value and the hazard settings
were `default`. The attempted values were day `onlyday`, hunger and temperature
damage `nonlethal`, winter/spring/summer `noseason`, hounds/shadow creatures/weather/
lightning `never`. This is concrete config persistence/validation evidence; it is not
a reward eligibility finding.

Production LONG_SESSION task 4 / job 78 completed with `TIME_BUDGET_REACHED`. Fresh
monitoring ran from `2026-09-29T20:26:52.892900Z` to
`2026-09-29T20:36:58.741434Z` (about 10 minutes), with 51 fresh in-world checkpoints,
zero recoveries, no manual gameplay intervention, and the gift icon continuously
classified `NO_REWARD_AVAILABLE`. The player remained `IN_WORLD_IDLE` at the final
fresh checkpoint. This is a real operational idle soak, but not a safe-profile soak.

After cleanup, runtime 1 / `dst-000001-g1` is STOPPED, GameWorker is DISABLED,
`held_inputs=false`, and Job 78 is SUCCEEDED. Read-only post-stop save verification
found Master `EA6E12E4296C650B`, save `0000000003`, exactly one Science Machine at
`(374, 196)`, and no temporary mods. The prepared rollback snapshot/archive, compact
Stage 3D save archive, and original config archive remain available. Host filesystem
free space was about 7.05 GB at the final checkpoint; the 2 GiB guard is implemented.

The 75–90 minute session was not started because live safe-setting persistence failed.
Reward eligibility remains `SAFE_WORLD_REWARD_ELIGIBILITY_NOT_YET_OBSERVED`; the gray
icon does not establish that safe settings disable rewards. No 6–10 hour soak ran.

Implementation and focused/regression tests are in the checkpoint. The requested
targeted suite passed 194 tests. Ruff passed; `git diff --check` passed. Format check
passed for 21 changed Python files; seven files retain formatting drift and were
left untouched. The safe-profile ownership and evidence blocker was closed in Stage
4A.2 below; reward eligibility and longer survival validation remain separate.

## Stage 4A.2 — safe world config ownership and live evidence (2026-09-29)

The installed DST build is `747465` (Steam build ID `24700692`). Its
`scripts/tools/generate_worldgenoverride.lua` writes `worldgenoverride.lua` with
`override_enabled` and partial `overrides`; `scripts/map/customize.lua` exposes the
safe profile's keys and values. The prepared world's `Master/leveldataoverride.lua`
is a complete saved level definition and is no longer the safe-profile ownership
layer.

The canonical safe profile now renders deterministically to the cluster-level
`worldgenoverride.lua`. Runtime Agent reconciliation runs in the existing DST
`ProcessSupervisor.before_start` path on every launch/restart and is idempotent.
Post-ready evidence carries desired/applied SHA-256, account/runtime ID and generation,
cluster path, DST PID/start ticks, process generation, and verification timestamp.
Runtime Agent refreshes that evidence only after checking the current file and process
identity; the Control Plane checks it against the current runtime before admitting
worker activation. `LongSession` also rejects missing, stale, mismatched, or unverified
evidence throughout monitoring. The evidence explicitly records that world behavior
is not verified.

Live validation deployed revision `db938d55ab4f3f1a66fb9ae33e3907c0c2ce512d`, then
used managed STOP/START. Fresh guest and Control Plane hashes matched
`c6a17368c19966a901c778b70a8b4b9b28ba71615034f4a183543c83908c1b83` for runtime 1,
generation 1, DST PID 891/start ticks 5149068. `LONG_SESSION` entered the prepared
world and completed its 600-second monitoring budget with 51 checkpoints and zero
recoveries. It finished `TIME_BUDGET_REACHED`; GameWorker ended DISABLED with no held
inputs, and managed STOP returned runtime 1 to STOPPED. The post-stop
`worldgenoverride.lua` hash still matched. This proves configuration-file/process
evidence and short unattended operation, not that DST applied each setting to the
existing save or that those settings guarantee survival/reward eligibility.

Focused tests passed (26); Ruff and `git diff --check` passed. The work is committed
and pushed to `main` in `43edc8b` and deployment fix `db938d5`.

## Safe-profile factual application correction (2026-09-29)

The requested 5400-second soak stopped after 16m04s/80 checkpoints: dusk occurred
and DST logged `Not applying world gen overrides`, despite file/process evidence.
No death/reset or unexpected input was observed. This invalidated admission based
only on configuration-file verification.

Installed-build audit and live provisioning confirmed the supported correction:
USER overrides belong in `Cluster_1/Master/worldgenoverride.lua` for this hosted
shard; DST also applies these world settings when loading an existing save.
The same prepared session `EA6E12E4296C650B` was loaded with all ten actual override
markers and saved normally as `0000000004`. Persisted settings match the canonical
profile; persisted clock is day=16/dusk=0/night=0, matching the visible all-day HUD.
No regeneration/mod/console or new input path was needed.

The prepared Master now has a versioned safe manifest and a protected rollback/
restore archive. Runtime evidence distinguishes CONFIG_PRESENT from
WORLD_PROFILE_VERIFIED and current loaded-world application. LongSession requires
persisted safe settings/fixture identity and current-process load evidence at
in-world checkpoints. Old unsafe snapshots cannot satisfy that contract.
See [the installed-build finding and provisioning contract](docs/SAFE_WORLD_PROFILE.md).
Deployed-code short validation is recorded separately in the stage's live artifacts;
the 75–90 minute soak remains pending and must not start automatically.

## Do not do now

- No multi-worker/orchestration work.
- No generic foundation hardening.
- No navigation/pathfinding.
- No farming/combat/survival AI.
- No repeated clean `MAIN_MENU` replay unless affected code changed.
- No GUI-console diagnostic as the normal gift-development path.
- No generic behavior-tree/planner framework.
- No full test suite after every small patch.

## Stage 4B — installed-build reward mechanics audit (2026-09-30)

Build-specific audit: DST `747465` / Steam build `24700692`. The bundled Lua clearly
separates lobby daily gifts from in-world playtime gifts. Lobby login waits for
inventory download / `CHECK_DAILY_GIFT` and presents `GetDailyGiftItem()` plus
unopened entitlements in `ThankYouPopup`. In-world gifts use native
`TheInventory:GetClientGiftCount`, replicated `hasgift`, and a separate
`hasgiftmachine` state.

The previous assumption that a gray present means `NO_REWARD_AVAILABLE` is disproven
by the installed client flow. `GiftItemToast` is displayed when a gift count exists,
but it is click-disabled until `builder.lua` finds a visible nearby `giftmachine`
while the player's inventory is open. Science Machine and Alchemy Engine provide
that tag. The retained live gray-present frame is therefore evidence of a pending
gift without an enabled machine context; the 48m06s segment does not establish a
no-gift AFK interval. Native account eligibility/timer logic is not in the scripts
bundle. The current source/runtime findings and bounded next experiment are in
[REWARD_MECHANICS_AUDIT_20260930.md](docs/REWARD_MECHANICS_AUDIT_20260930.md).

This checkpoint adds a separate `inworld_gift_state` telemetry field and reports
gray visible present as `IN_WORLD_GIFT_PENDING`. One live canonical `Tab`/
`OPEN_INVENTORY` action was fresh-screen-change verified, but the toast stayed gray.
Build source shows this key opens `CONTROL_OPEN_INVENTORY`, not the crafting menu
needed for the giftmachine context. Production ACTIVE mode now proposes one bounded
canonical `OPEN_CRAFTING_MENU` action on `b` (`CONTROL_OPEN_CRAFTING`) for the next
live check. The production gift click remains unproven and must not be treated as the
daily `LOGIN_REWARD_AVAILABLE` modal. Actual `GiftItemPopUp` result, claim
confirmation, daily-vs-weekly durable progression, account weekly count, AFK
eligibility, and this account's next-due/reset timestamps remain open.

Runtime health evidence at 2026-09-30 13:50 UTC: DST and Steam running, worker ACTIVE,
no held inputs, zero world-interaction actions before this stage, and item-server
HealthCheck returned `OK`. After deployment, the worker opened the canonical
inventory once (0.52s; fresh UI change verified); the toast remained gray. The agent
adoption preserved Xvfb/Steam/DST process identities. The currently pending `b` /
`OPEN_CRAFTING_MENU` fix is offline tested but not yet deployed or exercised live.
HealthCheck is service health only; it does not prove authenticated item-server
eligibility or counted playtime.

### Stage 4B live follow-up (2026-09-30)

The `OPEN_CRAFTING_MENU` action was deployed and sent once through GameWorker; its
generic pixel-change verification did not prove that the crafting menu opened. Installed
`GiftItemToast` source shows `PlayerHud:OpenCrafting` hides toast items until
`CloseCrafting`. A later read-only screenshot and fresh GameWorker telemetry at
14:14:49 UTC showed the gift toast visible and gray (`IN_WORLD_GIFT_PENDING`,
`chroma_p95=13`, `colored_fraction=0`), so there is still no actionable live sample.
That later frame cannot establish whether the menu opened briefly and then closed.

`OPEN_CRAFTING_MENU` transition verification requires the verified
`world_present_banner` detection to disappear in a fresh frame; a generic screen change
cannot complete the action. Commit `f9a18da` was deployed at 14:18:07 UTC without
changing Steam/DST/Xvfb PIDs. Its single bounded `b` press timed out after 8s; the toast
remained visible and gray. GameWorker safely fell back to `OBSERVE`/`NEEDS_ATTENTION`,
with held inputs false.

Further source audit found `inventory_replica.lua` schedules server-side
`Inventory:Open()` at player creation. `Tab` opens/closes controller inventory UI;
`b` opens/closes crafting UI. Neither proves gift-machine eligibility. With the gray
toast, `hasgiftmachine` is false. The latest screenshot shows the prepared Science
Machine below the player, so the next bounded experiment is one 0.45-second canonical
step toward it. The station approach was deployed and used in two bounded steps, each verified from a
fresh gameplay ROI. A pink/red ACTIVE gift was then captured; see Stage 4C below. The
13:50 item-server HealthCheck is endpoint-only evidence. No login or in-world claim,
eligible-play duration, weekly progress, or AFK comparison is proven.

## Historical documentation

Dated stage, progress, handoff, and validation documents are historical evidence. They
are not current task authority unless the user explicitly names one. Use this document
for current status and blockers.


## Stage 4C — live active-present regression and current status (2026-09-30)

At 14:30:30 UTC, a real 1280x720 screenshot and 58x58 present crop were saved as
`runtime_agent/gameworker/dst/assets/samples/gift_icon_active_in_world_live.png` and
`gift_icon_active_live.png`. The active template matches at 0.99994 confidence. The
gray template scores 0.91665 and misses the icon; the old HUD classifier consequently
returned `UNKNOWN` despite a valid in-world HUD. Offline replay with the local patch
recognizes `IN_WORLD_IDLE` and `GIFT_AVAILABLE`; daily login state remains `UNKNOWN`.

The local change adds the verified active crop as an independent HUD anchor and makes
`CLICK_GIFT_ICON` track only in-world opening/received states, leaving daily login state
separate. The matching corpus fixture and focused regression are added. Commit
`003e1fa` contains this patch and the live regression fixture. The deploy script
installed metadata for this revision, but two consecutive deploy attempts timed out
without proving the new Runtime Agent startup/adoption heartbeat. At the last live heartbeat the GameWorker was OBSERVE /
NEEDS_ATTENTION and held inputs were false. Runtime Agent heartbeats stopped during the
reload attempts and Control Plane currently marks the runtime STALE. A subsequent
read-only screenshot still showed the active gift, live Wilson, and Day 37;
Xvfb/Steam/DST process identities were unchanged. The real `GiftItemPopUp`/`Use Later` transition still
lacks a live fixture, and no gift click has been sent. Exact activation time,
AFK-vs-active requirement, eligible-play interval, durable in-world claim, repeated
claim, daily claim, and weekly target/reset remain unknown.
