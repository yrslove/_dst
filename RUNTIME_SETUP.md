# Runtime setup

`CURRENT_IMAGE_VERIFIED=1` is a deliberate release gate: only a registered,
verified image may provision an ordinary Incus runtime. The local mock fixture is
the sole code-only exception. Real Linux behavior remains `UNVALIDATED_ON_REAL_NODE`.

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
9. Integrate launcher readiness probes that atomically create STEAM_READY_FILE and DST_READY_FILE only after functional checks.
10. Enable auto-launch if desired, confirm a fresh authenticated GAME_READY heartbeat, then queue VERIFY_RUNTIME. VERIFY does not accept only an Incus RUNNING state.
11. STOP the verified runtime, then validate later START and host reboot persistence.

RuntimeAgent does not infer readiness from PID alone. It stores PID/start/exit/restart diagnostics in heartbeat details and stops restarting after the configured bound.

Rebuild creates generation + 1 and returns to NEEDS_LOGIN. Steam credentials/session are not copied automatically.

## Worker and VIEW image requirements

Install runtime-only dependencies from `requirements-runtime.txt`, `xdotool`, xpra
with HTML5 support, Xvfb, and the verified launcher/readiness integration in the
versioned image. Keep `WORKER_MODE=DISABLED` and `WORKER_AUTOSTART=0` during image
validation. `DISPLAY` in `/etc/dst-runtime/agent.env`, Xvfb, Steam, DST, xpra, capture,
and xdotool must refer to the same X display.

Do not add real account credentials or gameplay screenshots to the base image. Visual
templates are versioned runtime assets and must be captured from an authorized test
account, reviewed, and validated in OBSERVE mode.
