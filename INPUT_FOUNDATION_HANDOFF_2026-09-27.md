# DST input and action foundation — 2026-09-27

## Verified boundary and architecture

The existing authenticated Incus runtime `dst-000001-g1` remained running throughout this work. Its control-plane GameWorker remains `DISABLED` with `WORKER_AUTOSTART=0`; Steam, Klei/DST, Xvfb, and the Runtime Agent were not restarted. The acceptance run used a separate, action-whitelisted diagnostic process and ended at the real `MAIN_MENU`.

The old path was policy → `GameActions`/`ActionExecutor` → `InputController` → separate `xdotool` XTEST subprocesses → Xvfb `:99` → SDL/DST. Pointer motion caused the real hover effect. X11 button events and successful XTEST calls did not cause a UI transition. A small SDL harness saw no button event from the old short pulse, while manual CONTROL VIEW clicks and a headless xpra protocol client did produce button events. The precise SDL filtering rule is still unknown; the proven boundary is that X11 transport completion cannot be treated as SDL/game acceptance.

**Production choice: one private xpra shadow input channel per active runtime worker.** `XpraInputDriver` owns a system-Python bridge, which owns one xpra 3.1 shadow server for the runtime's local Xvfb display. The bridge uses a private mode-0700 temporary directory and Unix socket, no HTML client, no browser, no TCP binding, and an exclusive display lock. It sends xpra's normal `pointer-position`, `button-action`, and `key-action` packets, then an `info-request` barrier to learn only that the server processed the packet. It keeps the pointer/button sequence in one client session, focuses the DST window under the detector anchor, and releases held inputs when closed. The action layer owns click settle/press durations; behavior code never sees them.

Xpra's own shadow server uses `XTestPointerDevice` and `XTestFakeButtonEvent` with synchronization; its UI loop also manages pointer motion, focus, modifiers, and client ownership. Reusing that known-working path avoids cloning its semantics. A native XTEST implementation would be smaller but had already failed end-to-end and would require explaining SDL's boundary. A proper uinput device could eventually work, but needs kernel/device permission, mapping, and lifecycle work in every Incus container; a temporary attempt did not establish a working click. The private xpra socket supports headless operation and per-runtime isolation without a public port. Its memory cost should be measured before running many runtimes concurrently; the planned sequential account use has only one active input owner per runtime.

## Acceptance evidence

`scripts/diagnostics/menu_input_acceptance.py` uses the production `InputController`, `GameActions` executor with a three-action whitelist, `X11ScreenCapture`, `VisionDetector`, and `ActionLifecycle`. It never changes the service worker mode. The initial Options discard dialog from earlier diagnostics was safely cleared first.

| Action | Transport | Visual result, two consecutive real frames | Result |
| --- | --- | --- | --- |
| `CLICK_OPTIONS` from `MAIN_MENU` | `SENT` | `OPTIONS` 0.999929, 0.999929 | `SUCCEEDED` |
| `CLICK_BACK` from `OPTIONS` | `SENT` | `OPTIONS_DISCARD_CONFIRM` 0.999976, 0.999976 | `SUCCEEDED` |
| `DISCARD_OPTIONS` from confirmation | `SENT` | `MAIN_MENU` 0.976762, 0.970890 | `SUCCEEDED` |

DST considered the Options screen dirty even though this session did not deliberately edit a setting. The Back action therefore led to its real “Lose Changes?” modal. Clicking Yes discarded any pending Options changes and completed the return. No settings were saved. Every visual transition was bounded to 45 seconds because the current capture/detector path takes several seconds per frame; the first 15-second trial observed the menu but expired before two observations. There were no blind retries.

## API and state rules

Behavior provides an `ActionProposal(ActionName, reason=...)` after observing a verified screen. It does not provide pixels. The central `transitions.CONTRACTS` entry maps an action to source state, detector anchors, allowed target state, minimum confidence (0.94), two distinct post-action frames, and timeout. `click_request(action, observation)` resolves the detector's normalized anchor center and viewport; `GameActions.execute(action, target=..., viewport=...)` enforces a fixed allowed region. The `ObservePipeline` sends the action once and lets `ActionLifecycle` consume later observations.

Lifecycle: `PENDING` (ticket), `SENT` (transport only), `VERIFYING`, then `SUCCEEDED`, `FAILED`, or `TIMED_OUT`. `SUCCEEDED` requires two distinct fresh post-action frames, production-ready perception, matching runtime/worker generations, allowed target screen, and confidence at least 0.94. Duplicate frames, stale observations, and unexpected screens never count as success. Timeout or input failure sets the behavior intervention flag; no further gameplay action is proposed. A safety gate closing revokes the controller and releases inputs. `DISABLED` is the default; OBSERVE uses `ObserveActions` with no live input controller, and REPLAY uses a suppressing sink.

Currently real-frame supported screens are `LOGIN_REWARD_AVAILABLE`, `MAIN_MENU`, `OPTIONS`, and `OPTIONS_DISCARD_CONFIRM`. Reward opening has a contract but has **not** been validated through this new live input channel because the reward modal is no longer present. Other enum values are placeholders or lack verified assets.

To add a screen: save a real 1280×720 example under `dst/assets/samples`, crop stable anchors, register them in `dst/assets/manifest.json`, add a `VisionDetector.analyze` screen rule, and test the real sample plus likely confounders. To add a safe action: add an `ActionName`, allowed UI region, and `ActionContract` with source/target screens and anchors; let the pipeline derive the target and verify the transition. The service worker's executor whitelist currently permits only `CLICK_REWARD_OPEN`; adding a production action requires an explicit, narrow whitelist change after its own supervised verification. Keep account/world/destructive actions outside the whitelist until separately authorized and verified. Behavior should only use observation → state → permitted proposal → verified result.

## Tests and runtime commands

From repo root: `.venv/bin/pytest -q`; `.venv/bin/ruff check .`; `.venv/bin/python -m compileall -q app runtime_agent scripts/diagnostics`; `git diff --check`. The saved-frame transition test is `tests/unit/test_gameworker_transitions.py`. The SDL event probe is `scripts/diagnostics/sdl_input_harness.py`; it is a diagnostic tool, not a production backend.

Read-only runtime checks: `incus list dst-000001-g1 -c ns`; `incus exec dst-000001-g1 -- systemctl status dst-runtime-agent --no-pager`; `incus exec dst-000001-g1 -- sh -lc 'tr "\\0" "\\n" </proc/321/environ | grep -E "^(WORKER_MODE|WORKER_AUTOSTART)="'` (replace PID after agent restart); `incus exec dst-000001-g1 -- sudo -u dst env DISPLAY=:99 XAUTHORITY=/home/dst/.Xauthority xdotool getwindowfocus getwindowname`. Do not restart `dst-runtime-agent.service` just to reload worker code: its shutdown also stops Steam/DST/Xvfb. No runtime recreation, logout, public port, or remote push is needed.

The final source was copied into `/opt/dst-orchestrator` inside the existing runtime, but the long-running disabled worker child was deliberately not reloaded. Before any future live worker action, reload only that disabled child under supervision and verify it still reports `DISABLED`; do not restart the Runtime Agent service to accomplish this.

## Work for Luna

1. Keep the service worker `DISABLED`; implement one small behavior at a time using `ActionProposal` and `ActionContract`. Do not put xpra/X11 handling in behavior.
2. Wait for a naturally occurring reward modal, then validate `CLICK_REWARD_OPEN` with two post-action visual frames before treating it as supported.
3. Add saved-frame regression cases for each new state and action, including ambiguous/overlay frames and timeout handling.
4. Measure current capture/detector latency and private xpra memory before considering concurrent runtimes; reduce the 45-second verification window only with measured evidence.
5. Validate xpra keyboard `key-action` against an SDL harness before relying on movement or other keyboard behavior. Only mouse menu actions were accepted live here.

World creation, farming, scheduler, account rotation, combat, and exploration are outside this checkpoint.
