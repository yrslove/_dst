# DST Orchestrator Agent Rules

## Source of truth

The user prompt defines the current task. Implement only what is required.

### Current-state authority

For current work, read in this order:

1. `AGENTS.md`
2. `CURRENT_STATE.md`
3. `PROJECT_MAP.md`

Dated stage/progress/handoff/validation documents are historical evidence, not current task authority unless explicitly named by the user. Do not revalidate functionality listed as LIVE_PROVEN in `CURRENT_STATE.md` unless the task changes that path or there is concrete regression evidence.

Preserve the existing architecture. Prefer the smallest robust change over redesigns, speculative abstractions, or unrelated cleanup.

## Minimal-context workflow

1. Start from files, symbols, states, errors, logs, or components named by the task.
2. Search exact anchors before broad repository searches.
3. Inspect only directly relevant code first.
4. Do not read large files in full when the relevant function/block is sufficient.
5. Widen investigation only when evidence requires it.
6. Patch the smallest architectural layer that actually owns the problem.
7. Run focused tests before broad suites.

Do not repeatedly rediscover already established architecture.

## Core architecture

Preserve and reuse:

- Control Plane
- Runtime Agent
- GameWorker
- existing perception/state model
- canonical input/action path
- command/ACK lifecycle
- diagnostic recording/replay
- Incus runtime isolation
- Xvfb/xpra
- Steam/DST adapters
- ProcessSupervisor

Production behavior follows:

observe -> classify -> decide -> act -> fresh observe -> verify -> recover

## Non-negotiable invariants

- One canonical input mechanism.
- One perception/state system.
- Do not introduce production `xdotool` bypasses.
- Do not create a parallel command or ACK path.
- Avoid brittle full-screen templates and large collections of absolute coordinates.
- Actions must be bounded by timeout/retry limits.
- Release held input on success, failure, timeout, cancellation, disable, reset, and exception.
- Verify important actions from fresh post-action observations.
- Treat Loading, Paused, death/reset, and other non-actionable states explicitly.
- Reuse existing death/reset recovery; do not create a second recovery controller.
- Recording must remain bounded.
- Preserve useful real-world fixtures.
- Do not deliberately kill the character only for testing.
- Do not start multi-worker scaling until single-worker behavior is reliable.
- Do not build a generic gameplay-AI framework before simple deterministic primitives require it.

## Live runtime safety

The VPS may contain a live authenticated Steam/DST runtime.

Before lifecycle operations, understand what the command will stop or restart.

Do not assume restarting Runtime Agent preserves Steam/DST/Xvfb; current lifecycle may terminate managed processes.

Avoid destructive runtime/storage operations unless explicitly required.

Leave GameWorker/input ownership in a safe state after live validation.

Do not delete authentication/world data or diagnostic recordings indiscriminately.

## Behavior development

Prefer reusable primitives over one-off gameplay scripts. Do not implement full farming until required primitives are reliable.

Current priority:

1. Fix current correctness blockers.
2. First real gift claim.
3. Minimal single-account gift/playtime loop.
4. Unattended single-account reliability.
5. Only then scaling/orchestration.

## Investigation discipline

- Allow at most two evidence-based hypotheses before widening investigation.
- Do not start a new architectural investigation when one concrete blocker is already isolated.
- Live validation should exercise only the path changed by the current task.
- Do not replay `MAIN_MENU -> IN_WORLD` for unrelated changes.

## Performance

Runtime decisions must be deterministic and local.

Do not put an LLM in the gameplay hot path.

Avoid expensive per-frame work when ROI/state checks or action-level verification are sufficient.

Design for repeated unattended execution, not a single successful demo.

## Verification

For changed code:

- run the narrowest relevant tests;
- run Ruff when Python code changes;
- run `git diff --check`;
- review the touched diff for scope creep.

For live validation, clearly distinguish:
- proven against real DST;
- proven through replay;
- proven only by unit/integration tests.

Never claim live validation that did not happen.

## Git

Do not include secrets, credentials, runtime data, recordings, temporary files, or unrelated changes in commits.

Do not push unless explicitly requested.

## Final report

Keep reports compact:

Status: PASS | PASS_WITH_LIMITATIONS | BLOCKED

Changed:
- file — reason

Checks:
- command — result

Live:
- only actual live validation, if any

Limitations:
- concrete unresolved items only

Next:
- smallest logical continuation
