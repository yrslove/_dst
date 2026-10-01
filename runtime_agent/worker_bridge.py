from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from app.runtime.display import DisplayEnvironment
from runtime_agent.gameworker.base import WorkerContext, WorkerReport
from runtime_agent.gameworker.config import WorkerConfig
from runtime_agent.gameworker.process import WorkerProcessHost

logger = logging.getLogger("runtime_agent.worker_bridge")


class WorkerBridge:
    def __init__(
        self,
        account_id: int,
        runtime_id: int,
        display: DisplayEnvironment,
        config: WorkerConfig,
        *,
        max_restarts: int = 3,
        restart_backoff_seconds: float = 0.0,
        runtime_generation: int = 1,
    ):
        self.context = WorkerContext(
            account_id,
            runtime_id,
            display,
            runtime_verified=False,
            runtime_generation=runtime_generation,
        )
        self.config = config
        self._ready_called = False
        self._verified = False
        self._lock = threading.RLock()
        self._pending_commands: set[int] = set()
        self._completed_commands: OrderedDict[int, str] = OrderedDict()
        self._outgoing_acks: list[dict] = []
        self._host = WorkerProcessHost(
            config,
            self.context,
            max_restarts=max_restarts,
            restart_backoff_seconds=restart_backoff_seconds,
        )
        # NOOP deliberately crosses the same process/lifecycle boundary as DST.
        # This keeps infrastructure validation representative without game access.
        self._host.start()

    def _remember_completion(self, command_id: int, result: str) -> None:
        self._completed_commands[command_id] = result
        self._completed_commands.move_to_end(command_id)
        while len(self._completed_commands) > 1024:
            self._completed_commands.popitem(last=False)

    def set_runtime_verified(self, verified: bool) -> None:
        with self._lock:
            if verified == self._verified:
                return
            self._verified = verified
            self.context = WorkerContext(
                self.context.account_id,
                self.context.runtime_id,
                self.context.display,
                runtime_verified=verified,
                state=self.context.state,
                metadata=self.context.metadata,
                runtime_generation=self.context.runtime_generation,
            )
            self._host.update_context(self.context)
            self._host.command("RUNTIME_VERIFIED", value=verified)

    def on_game_ready(self) -> WorkerReport:
        with self._lock:
            self._ready_called = True
            self._host.command("GAME_READY")
            return self._host.tick()

    def tick(self) -> WorkerReport:
        with self._lock:
            return self._host.tick()

    def apply_commands(self, commands: list[dict]) -> None:
        with self._lock:
            for item in commands:
                try:
                    command_id = int(item["id"])
                    command = str(item["command"]).upper()
                    if command_id < 1 or not command:
                        raise ValueError
                except (KeyError, TypeError, ValueError):
                    logger.warning(
                        "discarding invalid worker command runtime_id=%s",
                        self.context.runtime_id,
                    )
                    continue
                if command_id in self._completed_commands:
                    logger.info(
                        "worker_ack_bridge_replay runtime_id=%s command_id=%s result=%s",
                        self.context.runtime_id,
                        command_id,
                        self._completed_commands[command_id],
                    )
                    self._outgoing_acks.append(
                        {
                            "id": command_id,
                            "result": self._completed_commands[command_id],
                        }
                    )
                    continue
                if command_id in self._pending_commands:
                    continue
                self._pending_commands.add(command_id)
                logger.info(
                    "worker_command_bridge_accepted runtime_id=%s command_id=%s "
                    "command=%s pending_after=%s",
                    self.context.runtime_id,
                    command_id,
                    command,
                    sorted(self._pending_commands),
                )
                if command == "STOP":
                    self._host.request_stop(command_id)
                elif command == "RESUME":
                    self._host.command(
                        "RUNTIME_VERIFIED", value=self.context.runtime_verified
                    )
                    if self._ready_called:
                        self._host.command("GAME_READY")
                    self._host.command(command, command_id=command_id)
                elif command == "SET_MODE":
                    payload = item.get("payload", {})
                    values = {"mode": payload.get("mode", "DISABLED")}
                    for key in ("locomotion_profile", "experiment_session_id", "experiment_seconds",
                                "experiment_until_gift", "experiment_target_valid_seconds",
                                "experiment_continue_after_claim"):
                        if key in payload:
                            values[key] = payload[key]
                    self._host.command(command, command_id=command_id, **values)
                else:
                    self._host.command(command, command_id=command_id)

    def pause(self) -> None:
        with self._lock:
            self._host.command("PAUSE")

    def on_game_lost(self) -> None:
        with self._lock:
            self._ready_called = False
            self._host.set_game_ready(False)

    def acknowledgements(self) -> list[dict]:
        with self._lock:
            for item in self._host.acknowledgements():
                command_id = int(item["id"])
                result = str(item["result"])
                pending_before = sorted(self._pending_commands)
                self._pending_commands.discard(command_id)
                self._remember_completion(command_id, result)
                self._outgoing_acks.append(item)
                logger.info(
                    "worker_ack_bridge_received runtime_id=%s command_id=%s "
                    "result=%s pending_match=%s pending_before=%s pending_after=%s",
                    self.context.runtime_id,
                    command_id,
                    result,
                    command_id in pending_before,
                    pending_before,
                    sorted(self._pending_commands),
                )
            values, self._outgoing_acks = self._outgoing_acks, []
            for item in values:
                logger.info(
                    "worker_ack_bridge_forward runtime_id=%s command_id=%s result=%s",
                    self.context.runtime_id,
                    item["id"],
                    item["result"],
                )
            return values

    def shutdown(self) -> WorkerReport:
        with self._lock:
            return self._host.shutdown()
