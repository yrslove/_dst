from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from app.providers.base import (
    InstanceNotFound,
    InstanceTimeout,
    ProviderStatus,
    ProviderUnavailable,
    ProvisionFailed,
    RuntimeDescriptor,
    RuntimeProvider,
)
from app.subprocess_env import sanitized_subprocess_environment

logger = logging.getLogger("provider.incus")


class IncusCLIProvider(RuntimeProvider):
    def __init__(self, settings):
        self.settings = settings

    def _target(self, runtime: RuntimeDescriptor, name: str | None = None) -> str:
        name = name or runtime.external_id
        remote = runtime.incus_remote or self.settings.incus_remote
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or (
            remote and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", remote)
        ):
            raise ProvisionFailed("invalid Incus instance or remote name")
        return name if remote in {"", "local"} else f"{remote}:{name}"

    def _run(
        self,
        *args: str,
        timeout: int | None = None,
        attempts: int | None = None,
        correlation_id: str | None = None,
        allow_failure: bool = False,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        timeout = timeout or self.settings.incus_command_timeout_seconds
        # Durable jobs retry mutations after a fresh inspect.
        attempts = attempts or 1
        last_error = ""
        for attempt in range(1, attempts + 1):
            logger.info(
                "incus command",
                extra={"event": "INCUS_COMMAND", "correlation_id": correlation_id},
            )
            try:
                process = subprocess.run(
                    ["incus", *args],
                    env=sanitized_subprocess_environment(),
                    text=True,
                    capture_output=True,
                    input=input_text,
                    timeout=timeout,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise ProviderUnavailable("incus executable not found") from exc
            except subprocess.TimeoutExpired as exc:
                if attempt == attempts:
                    raise InstanceTimeout(
                        f"incus command timed out after {timeout}s"
                    ) from exc
                time.sleep(min(2 ** (attempt - 1), 5))
                continue
            if process.returncode == 0 or allow_failure:
                return process
            last_error = f"incus {args[0]} failed (exit {process.returncode}); inspect node diagnostics"
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 5))
        raise ProviderUnavailable(last_error[:1000])

    def _exists(self, runtime: RuntimeDescriptor, name: str | None = None) -> bool:
        try:
            self.inspect(replace(runtime, external_id=name) if name else runtime)
            return True
        except InstanceNotFound:
            return False

    def ensure(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        if not self._exists(runtime):
            try:
                base = (
                    runtime.image_source_ref
                    or runtime.image_version
                    or self.settings.incus_base_instance
                )
                if base and self._exists(runtime, base):
                    self._run(
                        "copy",
                        self._target(runtime, base),
                        self._target(runtime),
                        correlation_id=correlation_id,
                    )
                elif base:
                    raise ProvisionFailed(
                        "configured versioned base instance does not exist"
                    )
                else:
                    args = ["init", self.settings.incus_image, self._target(runtime)]
                    if self.settings.incus_profile:
                        args.extend(["--profile", self.settings.incus_profile])
                    self._run(*args, correlation_id=correlation_id)
            except (ProviderUnavailable, InstanceTimeout) as exc:
                raise ProvisionFailed(str(exc)) from exc
        return ProviderStatus("NEEDS_LOGIN", {"instance": runtime.external_id})

    def inspect(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        process = self._run(
            "list",
            self._target(runtime),
            "--format",
            "json",
            timeout=30,
            correlation_id=correlation_id,
            attempts=self.settings.incus_max_attempts,
        )
        if process.returncode != 0:
            raise ProviderUnavailable("incus list failed; inspect node diagnostics")
        try:
            data = json.loads(process.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable("incus returned invalid JSON") from exc
        data = [item for item in data if item.get("name") == runtime.external_id]
        if not data:
            raise InstanceNotFound(runtime.external_id)
        state = str(data[0].get("status", "")).upper()
        if state not in {"RUNNING", "STOPPED"}:
            raise ProviderUnavailable(
                f"runtime has transitional or unknown Incus state: {state}"
            )
        return ProviderStatus(state, data[0])

    def start(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        status = self.inspect(runtime, correlation_id=correlation_id)
        if status.state != "RUNNING":
            self._run("start", self._target(runtime), correlation_id=correlation_id)
        return self.inspect(runtime, correlation_id=correlation_id)

    def stop(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        status = self.inspect(runtime, correlation_id=correlation_id)
        if status.state == "RUNNING":
            self._run(
                "stop",
                self._target(runtime),
                "--timeout",
                "30",
                correlation_id=correlation_id,
            )
        return self.inspect(runtime, correlation_id=correlation_id)

    def restart(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        status = self.inspect(runtime, correlation_id=correlation_id)
        if status.state == "RUNNING":
            self._run(
                "restart",
                self._target(runtime),
                "--timeout",
                "30",
                correlation_id=correlation_id,
            )
        else:
            self._run("start", self._target(runtime), correlation_id=correlation_id)
        return self.inspect(runtime, correlation_id=correlation_id)

    def destroy(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> None:
        if self._exists(runtime):
            self._run(
                "delete",
                self._target(runtime),
                "--force",
                correlation_id=correlation_id,
            )

    def rebuild(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        self.destroy(runtime, correlation_id=correlation_id)
        return self.ensure(runtime, correlation_id=correlation_id)

    def healthcheck(self, *, correlation_id: str | None = None) -> bool:
        process = self._run(
            "info",
            timeout=30,
            attempts=1,
            correlation_id=correlation_id,
            allow_failure=True,
        )
        return process.returncode == 0

    def execute(
        self, runtime, command, *, timeout=60, correlation_id=None
    ) -> ProviderStatus:
        if not command or any(
            not isinstance(value, str) or not value for value in command
        ):
            raise ProvisionFailed("runtime exec requires a non-empty argv")
        process = self._run(
            "exec",
            self._target(runtime),
            "--",
            *command,
            timeout=timeout,
            correlation_id=correlation_id,
        )
        return ProviderStatus(
            "EXITED",
            {
                "exit_code": process.returncode,
                "stdout": (process.stdout or "")[:4096],
                "stderr": (process.stderr or "")[:4096],
            },
        )

    def install_agent(self, runtime, *, correlation_id=None) -> None:
        # Reuse the existing validated runtime archive/installer and inventory.
        from scripts.deploy_runtime import (
            GUEST_INSTALLER,
            assert_clean_tree,
            make_metadata,
            run,
            runtime_files,
            write_archive,
        )

        repo = Path(__file__).resolve().parents[2]
        assert_clean_tree(
            run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=repo
            ).splitlines()
        )
        revision = run(["git", "rev-parse", "HEAD"], cwd=repo)
        installed = self._run(
            "exec",
            self._target(runtime),
            "--",
            "cat",
            "/opt/dst-orchestrator/DEPLOYMENT.json",
            allow_failure=True,
            correlation_id=correlation_id,
        )
        try:
            if json.loads(installed.stdout).get("commit") == revision:
                return
        except (ValueError, TypeError):
            pass
        active = self._run(
            "exec",
            self._target(runtime),
            "--",
            "systemctl",
            "is-active",
            "--quiet",
            "dst-runtime-agent.service",
            allow_failure=True,
            correlation_id=correlation_id,
        )
        if active.returncode == 0:
            raise ProvisionFailed(
                "agent upgrade requires an explicitly stopped runtime agent; managed processes are preserved"
            )
        files = runtime_files(repo)
        remote = "/tmp/dst-provision-agent.tar.gz"
        with tempfile.TemporaryDirectory(prefix="dst-provision-") as temporary:
            archive = Path(temporary) / "agent.tar.gz"
            write_archive(repo, files, make_metadata(revision, files, repo), archive)
            self._run(
                "file",
                "push",
                str(archive),
                f"{self._target(runtime)}{remote}",
                "--mode",
                "0600",
                correlation_id=correlation_id,
            )
            try:
                self._run(
                    "exec",
                    self._target(runtime),
                    "--",
                    "python3",
                    "-c",
                    GUEST_INSTALLER,
                    remote,
                    "/opt/dst-orchestrator",
                    revision,
                    "cold",
                    timeout=120,
                    correlation_id=correlation_id,
                )
            finally:
                self._run(
                    "exec",
                    self._target(runtime),
                    "--",
                    "rm",
                    "-f",
                    remote,
                    correlation_id=correlation_id,
                )

    def prepare_assets(self, runtime, *, correlation_id=None) -> None:
        source = self.settings.incus_runtime_assets
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", source) or ".." in source.split("/"):
            raise ProvisionFailed("unsafe runtime assets source")
        status = self.inspect(runtime, correlation_id=correlation_id)
        devices = status.raw.get("expanded_devices", status.raw.get("devices", {}))
        expected = {
            "type": "disk",
            "source": source,
            "path": "/opt/dst-runtime-assets",
            "readonly": "true",
        }
        existing = devices.get("runtime-assets")
        if existing:
            if any(existing.get(key) != value for key, value in expected.items()):
                raise ProvisionFailed(
                    "runtime assets device conflicts with cache contract"
                )
            return
        if any(device.get("path") == expected["path"] for device in devices.values()):
            raise ProvisionFailed("runtime assets path is already mounted")
        self._run(
            "config",
            "device",
            "add",
            self._target(runtime),
            "runtime-assets",
            "disk",
            f"source={source}",
            "path=/opt/dst-runtime-assets",
            "readonly=true",
            correlation_id=correlation_id,
        )

    def put_file(
        self, runtime, path, content, *, mode=0o600, correlation_id=None
    ) -> None:
        if (
            not re.fullmatch(r"/[A-Za-z0-9_./-]+", path)
            or ".." in path.split("/")
            or len(content) > 128 * 1024
        ):
            raise ProvisionFailed("unsafe runtime file injection request")
        # `-` makes incus file push consume stdin.  Content is deliberately never
        # included in the command arguments or logs.
        self._run(
            "file",
            "push",
            "-",
            f"{self._target(runtime)}{path}",
            "--mode",
            f"{mode:o}",
            timeout=60,
            correlation_id=correlation_id,
            input_text=content.decode("utf-8"),
        )
