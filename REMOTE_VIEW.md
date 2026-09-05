# Remote VIEW

Status: concrete xpra/Incus adapter implemented, `UNVALIDATED_ON_REAL_NODE`.

## Session flow

~~~text
authenticated admin + CSRF
  -> POST runtime view session (VIEW_ONLY or INTERACTIVE)
  -> random short-lived token; only SHA-256 hash stored
  -> xpra shadows the runtime's canonical DISPLAY
  -> ephemeral Incus proxy exposes xpra on control-plane loopback only
  -> token exchanged for a short-lived HttpOnly, path-scoped cookie
  -> FastAPI HTTP/WebSocket adapter proxies the xpra HTML5 transport
  -> explicit close or TTL cleanup removes proxy and xpra session
~~~

There is no permanent public VNC/xpra URL. Tokens are not put in URLs or logs.
The session model records admin user, runtime, token hash, backend/session ID, mode,
status, creation/expiry/access/close times, and bounded error details. Canonical states
are CREATING, ACTIVE, EXPIRED, CLOSED, and ERROR.

xpra uses `shadow`, not a second X server. Steam, DST, capture, input, and VIEW share
the same `DisplayEnvironment` (normally `DISPLAY=:99`). The xpra listener is bound to
container loopback, and its Incus proxy is bound to control-plane loopback; authorization
is enforced at the FastAPI adapter.

## Modes and interlock

VIEW_ONLY leaves worker state unchanged. INTERACTIVE queues durable PAUSE and withholds
transport until heartbeat confirms that input ownership was revoked. Pause releases
all held keys/buttons immediately in the worker process. Closing an interactive session
does not resume automation; the operator must use RESUME WORKER explicitly.

## Failure behavior

On Windows, without `incus`, without xpra in the runtime, with a missing X socket, or
with a non-Incus runtime, creation fails closed with
`REMOTE_VIEW_BACKEND_UNAVAILABLE`; no fake ACTIVE is returned. Mock is test-only and
forbidden in production settings. Expiry cleanup is bounded and periodic.

The first Linux validation must verify xpra CLI flags/version, Incus proxy devices,
HTML/WebSocket path behavior through the reverse proxy, TLS, reconnect/close cleanup,
and that no listener is reachable outside loopback.

The first concrete backend deliberately supports a control plane colocated with the
Incus Node (`incus_remote=local`). Named remote Incus Nodes fail closed instead of
returning a false ACTIVE because a remote host's loopback is not reachable by this
adapter. A future multi-Node deployment needs an authenticated Node-side relay or
outbound tunnel, designed after the one-Node transport is physically validated.
