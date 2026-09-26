# First Linux runtime validation — 2026-09-26

Status: **first real Linux OBSERVE recording and offline REPLAY complete**. Exactly
one GameWorker runtime exists, `dst-000001-g1`. ACTIVE input was never enabled.

## Running environment

- Host: Ubuntu 24.04 Azure VM, 2 vCPU, 7.7 GiB RAM, 4 GiB swap, 61 GiB OS disk.
- Incus 6.0 directory pool and bridge `10.119.21.1/24`. `dst-base-v1` is stopped;
  `dst-000001-g1` is running with 2 CPU / 5 GiB limits.
- Host-local control-plane and Node Agent systemd units are active. Control plane
  listens on **Incus bridge only** at `10.119.21.1:8080`; no public application,
  xpra, VNC, or debug listener was created. Local environment and SQLite files are
  ignored by Git under `.data/`, with private credentials stored there.
- Runtime Agent, Xvfb `:99`, DisplayEnvironment, supervised Steam/DST, and
  `DSTGameWorker` are running. The final worker mode is explicitly `DISABLED`;
  the control plane reports `GAME_READY`, and both readiness markers are fresh.
- Steam and DST were installed **inside this runtime**, not the base image.
  The temporary manual-login service is disabled. Steam signs back in from
  this runtime's private account data after Runtime Agent service restarts.
  No credentials or session data were copied into the base image.

## Actual validation and fixes

- Host node preflight passed. Base-image Xvfb `1280x720x24`, `xdpyinfo`, and a
  Pillow frame grab succeeded. Host Python venv and runtime dependencies are set up.
- Incus clone, token bootstrap, Node Agent and Runtime Agent authenticated
  heartbeats, and `BOOTSTRAP_COMPLETE` were observed on the real node.
- Runtime systemd Xvfb could not write `/tmp` under `ProtectSystem=strict`; the
  service now permits `/tmp`, and real display capture works.
- Ubuntu Steam package uses `/usr/games/steam`; runtime preflight now detects it.
  The runtime home `.local` ownership was corrected after the first Steam update
  failed. Steam required user namespace access inside the nested Incus guest; a
  narrowly scoped guest AppArmor profile for `srt-bwrap` was installed in the
  runtime, leaving the host restriction enabled. `dbus-x11` was added in the
  runtime for Steam's session bus. A real Steam sign-in window was visible on `:99`.
- VIEW_ONLY and INTERACTIVE xpra sessions reached the real X display through the
  loopback-only Incus proxy and control-plane HTTP adapter. The HTML page returned
  200; a WebSocket handshake was previously observed to open. Closing removed the
  proxy/session. Ubuntu xpra 3.1.5 did not accept its own `xpra stop`, so cleanup
  now terminates only the exact shadow process and verifies its listener closed.
  Ubuntu's xpra package also launched `/etc/X11/Xsession true` on each shadow
  session, leaving an error dialog on the game display. This runtime's
  `/etc/xpra/conf.d/99-dst-shadow.conf` now sets `start =` to suppress that
  command. A fresh VIEW open/close left no Xsession or xmessage process.
- On a browser test, CONTROL VIEW showed only xpra's blue background. Steam,
  steamwebhelper, and xpra were all alive on `DISPLAY=:99`; X11 reported the
  700x440 "Sign in to Steam" window mapped at `(290, 140)`, and both direct Xvfb
  and xpra screenshots contained it. The browser requested xpra's
  `js/lib/jquery.js` and got 404, so the HTML client never opened a WebSocket.
  Ubuntu xpra's symlink target came from the missing `libjs-jquery` package.
  Installing it **inside the existing runtime** made that asset return 200; all
  xpra web symlinks now resolve. The runtime and Steam session were preserved.
- The next browser session loaded HTML but stayed at "Opening WebSocket
  connection". The browser requested xpra's `binary` WebSocket subprotocol;
  FastAPI logged the upgrade as accepted, but the relay neither acknowledged
  that subprotocol to the browser nor requested it from xpra. Direct probes
  showed xpra rejected a bare WebSocket and accepted `binary`. The relay now
  negotiates `binary` on both legs. A real control-plane WebSocket returned
  HTTP 101, stayed open, and showed an ESTABLISHED TCP connection to xpra's
  loopback port. The regression test forwards a binary frame through both legs.
- A subsequent browser connection reached xpra but was disconnected with
  `server error / error accepting new connection`. Xpra's own `:99.log` showed
  the client hello reached its `127.0.0.1:14500` WebSocket listener, then the
  system Python process raised `ModuleNotFoundError: No module named 'PIL'`.
  Installing `python3-pil` inside this same runtime fixed it without restarting
  Steam, Xvfb, or the container. A real headless Chromium browser then opened
  an INTERACTIVE VIEW via the control-plane proxy, exchanged WebSocket frames
  in both directions, created the xpra canvas, and rendered the existing
  "Sign in to Steam" window. The test VIEW was closed afterward. Xpra 3.1.5
  logged a nonfatal window-icon resize error because Ubuntu's Pillow 10 removed
  `Image.ANTIALIAS`; the screen still rendered.
- INTERACTIVE VIEW issued an acknowledged PAUSE command. After DST launch,
  a fresh CONTROL VIEW session reached xpra's HTML5 canvas with one WebSocket,
  30 received and 33 sent frames. During OBSERVE, both PAUSE and RESUME were
  acknowledged and the worker returned to observing frames.
- A watchdog restart bug marked a new setup STALE by comparing the previous run's
  heartbeat. It now uses the current run start as the first-heartbeat baseline.
  Stopping an unverified runtime from NEEDS_ATTENTION now returns the account to
  NEEDS_LOGIN. Both paths have regression tests.

## Steam authentication, DST, and control-plane verification

- The operator authenticated Steam through CONTROL VIEW. Steam's logs recorded
  login success and a logged-on connection; no Steam Guard prompt appeared.
  Its main window became unmapped at `(0, -40)`, so we remapped and moved that
  existing window to `(0, 0)` without a runtime restart.
- The operator accepted Klei's EULA. Steam then finished installing DST app
  `322330`; its app manifest has `StateFlags=4` and `SizeOnDisk=4297870823`.
  Launching through the authenticated Steam client produced the native
  `dontstarve_steam_x64` process and a mapped 1280×720 DST main-menu window on
  `:99`. The `Thanks for playing / Open Now!` promotional modal is still
  visible. It does not block capture, VIEW, or REPLAY, and no automated input
  was sent to it.
- The existing Runtime Agent reads launcher settings only at startup. One
  controlled **service** restart enabled the readiness-aware Steam and DST
  launchers; the Incus container, Steam account data, and installation were
  preserved. Steam reauthenticated automatically. Fresh `steam.ready` and
  `dst.ready` markers and a `GAME_READY` heartbeat were observed with the
  worker still `DISABLED`.
- A control-plane fix now moves an unverified account from `NEEDS_LOGIN` to
  `VERIFYING` on a healthy supervised `STEAM_READY` heartbeat, without falsely
  setting `verified_at`. The first two VERIFY jobs exposed missing
  `VERIFYING -> RUNNING` and `ERROR -> RUNNING` recovery transitions. Those
  were fixed and tested; job #11 succeeded. Final account and runtime state
  are `RUNNING`, and `verified_at` is set.

## OBSERVE recording and offline REPLAY

- With Stage 4 recording configured, GameWorker started in `OBSERVE` but
  `PAUSED` (`WORKER_AUTOSTART=0`). A control-plane RESUME command was
  acknowledged. Real X11 frames were captured from the DST main menu. PAUSE
  and RESUME were each acknowledged during the run. STOP gracefully finalized
  exactly one recording session under
  `/home/dst/.local/state/dst-runtime/worker-recordings/ed875f94b39147069af2bcfb64c74918`.
- Its manifest is `COMPLETE`: **30 PNG frames**, **75 events** (including 30
  `FRAME_CAPTURED` and 30 `OBSERVATION_PRODUCED`), and **23,712,551 bytes**.
  Limits were 180 seconds, 80 frames, and 256 MiB; it closed before any limit
  truncated it. A sampled frame was visually confirmed to contain the DST menu
  and promotional modal, not an empty desktop. No temporary recorder files or
  second session remain.
- The existing `DSTGameWorker` REPLAY mode loaded and strictly validated that
  same COMPLETE session, processed all **30 frames** through the Stage 3
  perception pipeline, and produced **30 observations**. Its input controller
  and live actions were absent; REPLAY reported input forbidden. Perception
  outcomes were `UNKNOWN` because this runtime's visual templates/calibration
  are unverified. This proves recording and replay of real frames, not reliable
  gameplay recognition. One live `CAPTURE_ERROR` event occurred amid the
  pause/resume sequence; the other 30 frames replayed without capture or
  perception failure.
- `SET_MODE DISABLED` was initially rejected because recording-enabled config
  validation forbade DISABLED. Source code now permits a dormant recording
  configuration in DISABLED and closes recorder/capture/input resources on the
  mode change; a regression test verifies COMPLETE finalization. The current
  runtime was finally restarted with recording off and worker explicitly
  `DISABLED`. No ACTIVE mode or gameplay input was used.

## Resource measurements

| Condition | RAM | CPU | Disk |
| --- | ---: | ---: | ---: |
| Display/agent before Steam | ~167 MiB | — | — |
| Steam sign-in | ~876–921 MiB | — | — |
| DST menu, before OBSERVE | 4.01 GiB | — | — |
| Final DST menu, DISABLED worker | 3.89–3.90 GiB | 1.82 of 2 vCPU over 5 seconds (91% of VM capacity) | Runtime directory 9.8 GiB |

The final sample had 192 processes and an active xpra CONTROL VIEW. A previous
five-second sample reported 3.96–4.03 GiB and 1.44 CPU cores. The native game
process accounted for about 131% CPU in `ps`; this headless Xvfb environment
uses software rendering. These are short samples at the menu, not gameplay
benchmarks. DST's installed directory is 4.1 GiB, the recording directory is
23 MiB, and the base image is 2.3 GiB. The host root filesystem used 22/61 GiB
with 40 GiB free; host RAM available was about 3.9 GiB and swap use about
54 MiB. The runtime has a 5 GiB memory limit.

## Fixes and fresh-runtime reproducibility

| Fix or requirement | Where it currently lives | Fresh runtime from today's base image |
| --- | --- | --- |
| Xvfb `/tmp` write access, Steam `/usr/games/steam` preflight, watchdog heartbeat baseline, state transitions, xpra process cleanup and WebSocket `binary` forwarding | Repository source/tests | Source changes are repeatable, but the image must be rebuilt or updated to carry guest code. |
| Readiness-aware Steam/DST launchers, OBSERVE-to-DISABLED recorder cleanup | Repository source; manually copied to this runtime | Guest code and launcher environment settings must be installed on a fresh image/runtime. Current bootstrap validates agent files but does not install these additions. |
| `dbus-x11`, `libjs-jquery`, `python3-pil`, narrow guest AppArmor profile for Steam `srt-bwrap`, xpra `start =` override | This runtime only; requirements documented in `RUNTIME_SETUP.md` | Must be installed/configured again or baked into a new reviewed image. |
| Steam client, DST files, Steam session, Klei EULA acceptance | This runtime's private home only | Steam/Klei authentication and EULA acceptance remain manual per fresh runtime; credentials and sessions must not enter a shared base image. |

The `dst-base-v1` image is **not production verified**. Local
`CURRENT_IMAGE_VERIFIED=true` only permitted this first validation runtime
through the existing provision gate. Fresh clone and host-reboot persistence
have not been revalidated with all runtime-only fixes baked in. Visual assets
and calibration remain unverified, so OBSERVE does not establish reliable game
recognition or authorize ACTIVE.

Final repository checks: **192 passed, 2 skipped**; Ruff, Python compileall,
and `git diff --check` passed. The final control-plane heartbeat is
`GAME_READY`, with account/runtime `RUNNING` and GameWorker `DISABLED`. No Git
push or Azure network/security rule change was made.

## Final behavior/input checkpoint — 2026-09-26

- Captured a real Options frame at `runtime_agent/gameworker/dst/assets/samples/options_live.png`; added `options_title` and `options_back` templates and an `OPTIONS` classification requiring both anchors. The MAIN_MENU detector now also requires the Options label template. Offline checks against the real Options and MAIN_MENU frames classified them as `OPTIONS` and `MAIN_MENU` with confidence 0.9999 and 0.9972, respectively.
- The real screen was returned to MAIN_MENU after our Escape keypress opened the “Lose Changes? Do you want to throw out your changes?” dialog. No setting input was issued. Tab was sent once and the dialog closed; the subsequent screenshot showed MAIN_MENU. The exact reason Options was considered dirty is unknown.
- Xpra source inspection shows the shadow server's default pointer device is `XTestPointerDevice`; its button method calls `XTestFakeButtonEvent` inside an `XSync` wrapper. GameWorker uses separate `xdotool` processes for button down and up; xdotool queues XTEST edges and calls `XFlush`. Both target `:99`; the DST window/root geometry is 1280×720 at (0,0), and the final observed focus and pointer window were DST (`0x2a0000e`) at `(109,509)`. Focus/raise behavior and event delivery were not captured alongside an actual working CONTROL VIEW click, so the implementation differences above are not proven causal.
- A same-connection XTEST click with XSync and a 120 ms hold had already failed. A temporary uinput virtual mouse generated button press/release, but Xvfb exposed no `/dev/input` device and DST had no open evdev handle; the screen remained MAIN_MENU. A direct XSendEvent press/release to the focused DST window also left MAIN_MENU. Temporary Incus device mounts were removed.
- No XInput2/core event trace established whether DST received the button events. No GameWorker-driven MAIN_MENU→OPTIONS→MAIN_MENU probe succeeded, and no ACTIVE behavior was added. `WORKER_MODE=DISABLED`, `WORKER_AUTOSTART=0`; the runtime, Steam, DST, and Xvfb were not restarted. No reward popup was awaited.
