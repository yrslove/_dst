# Linux NODE setup

> This is a future operator procedure. It has not been executed by this code wave.

Node capabilities intentionally report `UNKNOWN` when the agent cannot establish
a real-node fact. Do not infer display, GPU, or remote-view availability from an
Incus `RUNNING` state.

1. Install and initialize Incus directly on the host; do not run the Incus daemon inside Docker.
2. Configure and name storage pool/network.
3. Validate cgroups, RAM/disk and GPU devices.
4. Run python scripts/node_preflight.py with site-specific minimums and required ports.
5. Register/configure the Node record and hard max_active_slots.
6. From an authenticated admin session call POST /api/v1/nodes/{id}/token/rotate.
7. Put the shown-once token in /etc/dst-orchestrator/node-agent.env mode 0600.
8. Create the dedicated `dst-node` service user, add it to the host's `incus-admin` group, and place its Incus client configuration in `/etc/dst-orchestrator/incus-node` with owner `dst-node` and mode 0700. Do not run the agent as root.
9. Install/enable deploy/systemd/dst-node-agent.service.
10. Confirm Node heartbeat/resources in UI before enabling real START.

Drain prevents new starts but lets existing runtimes continue. ENTER MAINTENANCE is rejected while active runtimes remain. DISABLE also requires zero active runtimes.

max_active_slots is a hard safety limit, not an estimate. RuntimeResourceProfile values remain unset until measurements exist.

Base images are versioned: dst-base-v1, dst-base-v2. Put OS, graphical stack, Steam binaries and agents in the image, never reusable real-account credentials.
