# Runtime setup

`CURRENT_IMAGE_VERIFIED=1` is a deliberate release gate: only a registered,
verified image may provision an ordinary Incus runtime. The local mock fixture is
the sole code-only exception. One Ubuntu 24.04 Incus runtime has now completed
real OBSERVE recording and offline REPLAY; see `LINUX_VALIDATION_2026-09-26.md`.
The current base image is still not production verified.

Provisioning creates a persistent Incus instance from the configured versioned base image and leaves it NEEDS_LOGIN.

Inside the instance:

1. Install a graphical session and verify DISPLAY/X11 socket.
2. Install Steam and DST.
3. Install the repository/venv and runtime systemd unit.
4. Rotate the runtime token through POST /api/v1/runtimes/{id}/token/rotate and store it mode 0600.
5. Keep AUTO_LAUNCH_STEAM and AUTO_LAUNCH_DST disabled for initial manual validation.
6. Queue `SETUP_RUNTIME` (or use the UI SETUP action). It consumes a normal Node slot and boots the otherwise unverified container.
7. Run scripts/runtime_preflight.py INSTANCE --control-plane-url HTTPS_URL while that setup runtime is running.
8. Manually authenticate Steam/Klei; do not bake session data into the base image.
9. Install the repository's `runtime_agent.launchers` module in the guest and
   configure `STEAM_COMMAND` and `DST_COMMAND` to run its `steam` and `dst`
   entry points. They create generation-scoped readiness markers after Steam
   logs on and DST has a visible X11 window. The current bootstrap validates
   guest agent files but does not copy this new module into an older base image.
10. Enable auto-launch if desired, confirm a fresh authenticated GAME_READY heartbeat, then queue VERIFY_RUNTIME. VERIFY does not accept only an Incus RUNNING state.
11. STOP the verified runtime, then validate later START and host reboot persistence.

For the first live recording, keep `WORKER_AUTOSTART=0`. A recording-configured
OBSERVE worker is paused at game readiness; RESUME explicitly, then DISABLE it
before the configured recording limits are exhausted so Stage 4 can write a
`COMPLETE` session for strict offline REPLAY. A limit-exhausted session is
`TRUNCATED` and strict REPLAY rejects it. The first real validation produced one
30-frame COMPLETE session. Its perception remained `UNKNOWN` because the visual
assets and calibration have not been verified.

RuntimeAgent does not infer readiness from PID alone. It stores PID/start/exit/restart diagnostics in heartbeat details and stops restarting after the configured bound.

Rebuild creates generation + 1 and returns to NEEDS_LOGIN. Steam credentials/session are not copied automatically.

## Worker and VIEW image requirements

Install runtime-only dependencies from `requirements-runtime.txt`, `xdotool`, xpra
with HTML5 support, `libjs-jquery`, system `python3-pil`, Xvfb, and the verified launcher/readiness integration in the
versioned image. Keep `WORKER_MODE=DISABLED` and `WORKER_AUTOSTART=0` during image
validation. `DISPLAY` in `/etc/dst-runtime/agent.env`, Xvfb, Steam, DST, xpra, capture,
and xdotool must refer to the same X display.

On Ubuntu 24.04, the xpra HTML5 package symlinks
`/usr/share/xpra/www/js/lib/jquery.js` to the file supplied by `libjs-jquery`
without requiring that package. If it is absent, the HTML page loads but does not
open its WebSocket or display the shadowed screen. Check the file and a real
`GET /js/lib/jquery.js` before declaring VIEW ready.

Xpra runs with the container's system Python, not the app venv. Without
`python3-pil`, a WebSocket upgrade can succeed and then xpra rejects the client
hello with `No module named 'PIL'` / `error accepting new connection`. The
runtime preflight checks both the HTML jQuery asset and system Pillow import.

For xpra shadow on Ubuntu 24.04, set `start =` (an empty value) in
`/etc/xpra/conf.d/99-dst-shadow.conf` inside the runtime image. The distribution default starts
`/etc/X11/Xsession true` even for `shadow`; in a headless runtime that creates an
error dialog over the Steam/DST display. An empty `--start=` CLI option does not
replace this default in xpra 3.1.5 because the option appends to the configured
commands. The control plane starts xpra as root inside the container, so a `dst`
user-only config is insufficient. Verify `xpra showconfig` as root before using VIEW.

Do not add real account credentials or gameplay screenshots to the base image. Visual
templates are versioned runtime assets and must be captured from an authorized test
account, reviewed, and validated in OBSERVE mode.
