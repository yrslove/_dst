from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet

from app.models import (
    Account,
    AccountState,
    Node,
    NodeStatus,
    RuntimeInstance,
    RuntimeState,
    utcnow,
)


class InvalidStateTransition(ValueError):
    def __init__(self, entity: str, source: str, target: str):
        super().__init__(f"invalid {entity} state transition: {source} -> {target}")
        self.entity = entity
        self.source = source
        self.target = target


RUNTIME_TRANSITIONS: Mapping[str, AbstractSet[str]] = {
    RuntimeState.NEW: {
        RuntimeState.PROVISIONING,
        RuntimeState.DESTROYING,
        RuntimeState.ERROR,
    },
    RuntimeState.PROVISIONING: {
        RuntimeState.NEEDS_LOGIN,
        RuntimeState.READY,
        RuntimeState.ERROR,
        RuntimeState.DESTROYING,
    },
    RuntimeState.NEEDS_LOGIN: {
        RuntimeState.STARTING,
        RuntimeState.STOPPING,
        RuntimeState.STOPPED,
        RuntimeState.READY,
        RuntimeState.DESTROYING,
        RuntimeState.ERROR,
    },
    RuntimeState.READY: {
        RuntimeState.STARTING,
        RuntimeState.STOPPING,
        RuntimeState.STOPPED,
        RuntimeState.DESTROYING,
        RuntimeState.ERROR,
    },
    RuntimeState.STARTING: {
        RuntimeState.RUNNING,
        RuntimeState.STOPPING,
        RuntimeState.ERROR,
        RuntimeState.STALE,
    },
    RuntimeState.RUNNING: {
        RuntimeState.STOPPING,
        RuntimeState.STALE,
        RuntimeState.ERROR,
    },
    RuntimeState.STOPPING: {
        RuntimeState.STOPPED,
        RuntimeState.ERROR,
        RuntimeState.STALE,
    },
    RuntimeState.STOPPED: {
        RuntimeState.NEEDS_LOGIN,
        RuntimeState.READY,
        RuntimeState.STARTING,
        RuntimeState.PROVISIONING,
        RuntimeState.DESTROYING,
        RuntimeState.ERROR,
    },
    RuntimeState.STALE: {
        RuntimeState.STARTING,
        RuntimeState.STOPPING,
        RuntimeState.RUNNING,
        RuntimeState.STOPPED,
        RuntimeState.ERROR,
        RuntimeState.DESTROYING,
    },
    RuntimeState.ERROR: {
        RuntimeState.PROVISIONING,
        RuntimeState.STARTING,
        RuntimeState.STOPPING,
        RuntimeState.STOPPED,
        RuntimeState.DESTROYING,
        RuntimeState.NEEDS_LOGIN,
    },
    RuntimeState.DESTROYING: {RuntimeState.DESTROYED, RuntimeState.ERROR},
    RuntimeState.DESTROYED: set(),
}

ACCOUNT_TRANSITIONS: Mapping[str, AbstractSet[str]] = {
    AccountState.NEW: {
        AccountState.PROVISIONING,
        AccountState.DISABLED,
        AccountState.ERROR,
    },
    AccountState.PROVISIONING: {
        AccountState.NEEDS_LOGIN,
        AccountState.READY,
        AccountState.ERROR,
        AccountState.DISABLED,
    },
    AccountState.NEEDS_LOGIN: {
        AccountState.RUNNING,
        AccountState.PROVISIONING,
        AccountState.VERIFYING,
        AccountState.READY,
        AccountState.NEEDS_ATTENTION,
        AccountState.DISABLED,
        AccountState.ERROR,
    },
    AccountState.VERIFYING: {
        AccountState.READY,
        AccountState.NEEDS_LOGIN,
        AccountState.NEEDS_ATTENTION,
        AccountState.ERROR,
    },
    AccountState.READY: {
        AccountState.QUEUED,
        AccountState.RUNNING,
        AccountState.DISABLED,
        AccountState.ERROR,
        AccountState.NEEDS_ATTENTION,
        AccountState.PROVISIONING,
    },
    AccountState.QUEUED: {
        AccountState.RUNNING,
        AccountState.READY,
        AccountState.NEEDS_ATTENTION,
        AccountState.ERROR,
        AccountState.DISABLED,
    },
    AccountState.RUNNING: {
        AccountState.PROVISIONING,
        AccountState.READY,
        AccountState.NEEDS_ATTENTION,
        AccountState.ERROR,
        AccountState.DISABLED,
    },
    AccountState.NEEDS_ATTENTION: {
        AccountState.READY,
        AccountState.QUEUED,
        AccountState.RUNNING,
        AccountState.PROVISIONING,
        AccountState.DISABLED,
        AccountState.ERROR,
    },
    AccountState.DISABLED: {
        AccountState.NEW,
        AccountState.NEEDS_LOGIN,
        AccountState.READY,
        AccountState.NEEDS_ATTENTION,
    },
    AccountState.ERROR: {
        AccountState.QUEUED,
        AccountState.PROVISIONING,
        AccountState.NEEDS_LOGIN,
        AccountState.READY,
        AccountState.NEEDS_ATTENTION,
        AccountState.DISABLED,
    },
}

NODE_TRANSITIONS: Mapping[str, AbstractSet[str]] = {
    NodeStatus.ONLINE: {
        NodeStatus.OFFLINE,
        NodeStatus.DRAINING,
        NodeStatus.MAINTENANCE,
        NodeStatus.ERROR,
    },
    NodeStatus.OFFLINE: {
        NodeStatus.ONLINE,
        NodeStatus.DRAINING,
        NodeStatus.MAINTENANCE,
        NodeStatus.ERROR,
    },
    NodeStatus.DRAINING: {
        NodeStatus.ONLINE,
        NodeStatus.OFFLINE,
        NodeStatus.MAINTENANCE,
        NodeStatus.ERROR,
    },
    NodeStatus.MAINTENANCE: {NodeStatus.ONLINE, NodeStatus.OFFLINE, NodeStatus.ERROR},
    NodeStatus.ERROR: {NodeStatus.ONLINE, NodeStatus.OFFLINE, NodeStatus.MAINTENANCE},
}


def _transition(
    entity: str,
    obj: object,
    target: str,
    transitions: Mapping[str, AbstractSet[str]],
) -> bool:
    source = str(getattr(obj, "state" if entity == "runtime" else "status"))
    target = str(target)
    if source == target:
        return False
    if target not in transitions.get(source, set()):
        raise InvalidStateTransition(entity, source, target)
    setattr(obj, "state" if entity == "runtime" else "status", target)
    if hasattr(obj, "updated_at"):
        obj.updated_at = utcnow()
    return True


def transition_runtime(runtime: RuntimeInstance, target: RuntimeState | str) -> bool:
    return _transition("runtime", runtime, str(target), RUNTIME_TRANSITIONS)


def transition_account(account: Account, target: AccountState | str) -> bool:
    return _transition("account", account, str(target), ACCOUNT_TRANSITIONS)


def transition_node(node: Node, target: NodeStatus | str) -> bool:
    return _transition("node", node, str(target), NODE_TRANSITIONS)


def can_transition_runtime(
    source: RuntimeState | str, target: RuntimeState | str
) -> bool:
    return str(source) == str(target) or str(target) in RUNTIME_TRANSITIONS.get(
        str(source), set()
    )
