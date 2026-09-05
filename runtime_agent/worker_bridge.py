from __future__ import annotations

from app.runtime.display import DisplayEnvironment
from runtime_agent.gameworker.base import WorkerContext, WorkerReport
from runtime_agent.gameworker.config import WorkerConfig
from runtime_agent.gameworker.noop import NoopGameWorker
from runtime_agent.gameworker.process import WorkerProcessHost


class WorkerBridge:
    def __init__(
        self,
        account_id: int,
        runtime_id: int,
        display: DisplayEnvironment,
        config: WorkerConfig,
        *,
        max_restarts: int = 3,
    ):
        self.context = WorkerContext(
            account_id, runtime_id, display, runtime_verified=False
        )
        self.config = config
        self._ready_called = False
        self._verified = False
        self._handled_commands: set[int] = set()
        self._completed_commands: dict[int, str] = {}
        self._outgoing_acks: list[dict] = []
        self._host = (
            WorkerProcessHost(config, self.context, max_restarts=max_restarts)
            if config.plugin == "dst"
            else None
        )
        self._noop = NoopGameWorker() if self._host is None else None
        if self._host:
            self._host.start()

    def set_runtime_verified(self, verified: bool) -> None:
        if verified == self._verified:
            return
        self._verified = verified
        self.context = WorkerContext(
            self.context.account_id,
            self.context.runtime_id,
            self.context.display,
            runtime_verified=verified,
        )
        if self._host:
            self._host.context = self.context
            self._host.command("RUNTIME_VERIFIED", value=verified)

    def on_game_ready(self) -> WorkerReport:
        self._ready_called = True
        if self._host:
            self._host.command("GAME_READY")
            return self._host.tick()
        assert self._noop is not None
        return self._noop.on_game_ready(self.context)

    def tick(self) -> WorkerReport:
        if self._host:
            return self._host.tick()
        assert self._noop is not None
        return (
            self._noop.tick(self.context) if self._ready_called else self._noop.status()
        )

    def apply_commands(self, commands: list[dict]) -> None:
        for item in commands:
            command_id = int(item["id"])
            if command_id in self._completed_commands:
                self._outgoing_acks.append(
                    {"id": command_id, "result": self._completed_commands[command_id]}
                )
                continue
            if command_id in self._handled_commands:
                continue
            self._handled_commands.add(command_id)
            command = str(item["command"]).upper()
            if self._host:
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
                    mode = item.get("payload", {}).get("mode", "DISABLED")
                    self._host.command(command, command_id=command_id, mode=mode)
                else:
                    self._host.command(command, command_id=command_id)
            elif command in {"PAUSE", "STOP", "RESUME", "SET_MODE"}:
                assert self._noop is not None
                if command == "SET_MODE":
                    result = "UNSUPPORTED_NOOP"
                else:
                    getattr(
                        self._noop, command.lower() if command != "STOP" else "shutdown"
                    )()
                    result = "OK"
                self._completed_commands[command_id] = result
                self._outgoing_acks.append({"id": command_id, "result": result})

    def pause(self) -> None:
        if self._host:
            self._host.command("PAUSE")
        else:
            assert self._noop is not None
            self._noop.pause()

    def on_game_lost(self) -> None:
        self._ready_called = False
        if self._host:
            self._host.set_game_ready(False)
        self.pause()

    def acknowledgements(self) -> list[dict]:
        if self._host:
            for item in self._host.acknowledgements():
                self._completed_commands[int(item["id"])] = str(item["result"])
                self._outgoing_acks.append(item)
        values, self._outgoing_acks = self._outgoing_acks, []
        return values

    def shutdown(self) -> WorkerReport:
        if self._host:
            return self._host.shutdown()
        assert self._noop is not None
        return self._noop.shutdown()
