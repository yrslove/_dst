# Real Linux NODE validation checklist

This is a future validation plan, not evidence of completed testing.

1. Prepare Ubuntu, Incus, storage/network profiles, and the authenticated Node Agent.
2. Build a versioned runtime image with runtime requirements, Xvfb, xdotool, xpra HTML5,
   Steam, DST, launcher probes, persistence paths, and resource limits.
3. Register the image unverified; provision one setup runtime and interrupt/retry each
   resumable bootstrap phase.
4. Validate Runtime Agent systemd startup and canonical `DisplayEnvironment` propagation.
5. Validate Xvfb process/socket and confirm Steam, DST, capture, xdotool, and xpra all
   use exactly that display.
6. Configure xpra VIEW. Confirm Windows/absent backend fails with
   REMOTE_VIEW_BACKEND_UNAVAILABLE, then on Linux validate VIEW_ONLY, token exchange,
   HTTP/WebSocket proxying, TLS, TTL, explicit close, cleanup, and loopback-only ports.
7. Manually log into Steam/Klei. Validate NEEDS_LOGIN and session persistence without
   storing credentials in worker/image/logs.
8. Launch DST and validate functional readiness markers; PID alone is not readiness.
9. Queue VERIFY only after fresh authenticated GAME_READY and verified image checks.
10. Keep GameWorker DISABLED. Validate runtime/worker heartbeat fields and STOP/START,
    bounded process restarts, Node reboot, persistence, and orphan-free shutdown.
11. Add versioned visual templates from the authorized test runtime. Switch to OBSERVE;
    verify every observation/confidence/UNKNOWN result and `would_execute` with zero
    keyboard/mouse events.
12. Validate VIEW_ONLY while OBSERVE runs. Then open INTERACTIVE VIEW and prove it stays
    CREATING until PAUSED, all keys/buttons are released, and closing does not auto-resume.
13. Explicitly select ACTIVE and RESUME for one short controlled session. Verify action
    rate/duration limits, normalized coordinates, input lease, stuck/freeze recovery,
    deadman release, diagnostics rotation, STOP WORKER independent from STOP RUNTIME,
    and worker crash isolation.
14. Validate optional GPU visibility/passthrough separately; no GPU PASS is implied by
    CPU/static checks.

Required sequence:

~~~text
Ubuntu -> Incus -> runtime image -> bootstrap -> agent -> display -> VIEW
-> manual Steam login -> DST -> VERIFY
-> GameWorker DISABLED -> OBSERVE -> validate observations
-> ACTIVE -> short controlled session
~~~

Only after this checklist may Linux/Incus/Xvfb/xpra/Steam/DST/input/vision items lose
their `UNVALIDATED_ON_REAL_NODE` status.
