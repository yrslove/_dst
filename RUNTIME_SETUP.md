# Runtime setup

`CURRENT_IMAGE_VERIFIED=1` is a deliberate release gate: only a registered,
verified image may provision an ordinary Incus runtime. The local mock fixture is
the sole code-only exception. One Ubuntu 24.04 Incus runtime has now completed
real OBSERVE recording and offline REPLAY; see `LINUX_VALIDATION_2026-09-26.md`.
The current base image is still not production verified.

## Canonical fresh-runtime provisioning (bootstrap v9)

The versioned base provides OS packages, the graphical stack and Python venv.
Bootstrap installs the committed Runtime Agent using the existing deployment
archive/inventory; it rejects upgrading an active older agent instead of silently
terminating a live authenticated session. Existing failed runtimes need an explicit
agent stop before the upgrade. A base containing only `/usr/games/steam` is not a
complete Steam client installation.

Publish shared binaries once on the Incus node with:

```sh
sudo .venv/bin/python scripts/build_runtime_assets.py \
  --steam-install /path/to/offline/Steam/installation \
  --destination /var/lib/dst-orchestrator/runtime-assets/dst-BUILD-v1
sudo ln -s dst-BUILD-v1 /var/lib/dst-orchestrator/runtime-assets/current
```

The source must be offline. The publisher copies an explicit program-file list,
excludes config/userdata/logs/authentication files and generated runtime state,
and constructs a fresh app manifest containing only common content metadata.
It refuses to overwrite a published version. `INCUS_RUNTIME_ASSETS` selects the
node-local cache (also on a remote Incus node); the provider mounts it read-only
at `/opt/dst-runtime-assets`. Do not switch `current` underneath active runtimes;
release content updates with a new cache path during explicit maintenance.

Existing stages now perform actual preparation:
`DISPLAY_CONFIGURED -> STEAM_RUNTIME_PREPARED -> DST_RUNTIME_PREPARED -> BOOTSTRAP_COMPLETE`.
Steam preparation creates a writable private Debian installation under `/home/dst`,
seeds only client program files, sets ownership and the conventional Steam links.
DST preparation links shared executable/data and creates private library metadata.
The agent starts only after both checks succeed; its Steam supervisor independently
gates startup on those prerequisites. Completed bootstrap rechecks/repairs content
without restarting the service or replacing account metadata.

Steam client self-update remains private. The multi-gigabyte DST directory is one
read-only node cache shared by all runtimes, with no per-account download or copy.
Steam game updates must be published as a new common content version, not written
through the read-only game link. Game execution follows authenticated Steam readiness.
Before login the existing account state and agent operational phase are `NEEDS_LOGIN`;
Incus/container lifecycle remains `RUNNING`. No new lifecycle enum is introduced.

Steam login/session, config, userdata, Klei acceptance/data, cluster/world and
worker/evidence remain private. Credentials never enter the cache or base image.
Keep `WORKER_MODE=DISABLED` and `WORKER_AUTOSTART=0` until explicit operator takeover.
Authenticate only after provisioning passes; Steam Guard requires operator input.

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
