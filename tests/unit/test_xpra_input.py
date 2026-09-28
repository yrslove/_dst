"""Private input bridge framing, environment, and cleanup without a live display."""
import os

from app.runtime.display import DisplayEnvironment
from runtime_agent.gameworker.xpra_bridge import (
    _server_argv,
    _server_input_capabilities,
    _xpra_mouse_log_tail,
)
from runtime_agent.gameworker.xpra_input import XpraInputDriver


def test_private_server_does_not_expire_during_a_paused_worker():
    argv = _server_argv(":99", "/tmp/dst-input-test")
    assert "--server-idle-timeout=0" in argv
    assert "--debug=mouse" in argv
    assert "--log-file=/tmp/dst-input-test/xpra.log" in argv
    assert "--readonly=no" in argv


def test_xpra_server_input_capabilities_accept_bytes_and_fail_closed():
    assert _server_input_capabilities(
        [b"hello", {b"readonly": 0, b"pointer": 1}]
    ) == (False, True)
    assert _server_input_capabilities(
        [b"hello", {"readonly": "false", "pointer": "true"}]
    ) == (False, True)
    assert _server_input_capabilities([b"hello", {}]) == (True, False)


def test_xpra_mouse_diagnostics_are_filtered_and_bounded(tmp_path):
    path = tmp_path / "xpra.log"
    path.write_text(
        "noise " * 5000 + "\n" + ("button-action mouse input event " * 40 + "\n"),
        encoding="utf-8",
    )

    result = _xpra_mouse_log_tail(path)

    assert 0 < len(result) <= 16
    assert all(len(line) <= 220 for line in result)
    assert all(any(word in line for word in ("button", "mouse", "input")) for line in result)
    assert len(str(result).encode()) < 8192


def test_private_bridge_uses_sanitized_env_and_framed_replies(monkeypatch):
    captured = {}

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            captured.update(argv=argv, **kwargs)
            stdin_read, stdin_write = os.pipe()
            stdout_read, stdout_write = os.pipe()
            self.stdin = os.fdopen(stdin_write, "wb", buffering=0)
            self.stdout = os.fdopen(stdout_read, "rb", buffering=0)
            self.stdin_read = stdin_read
            self.stdout_write = stdout_write
            os.write(
                stdout_write,
                b'{"ok":true,"ready":1,"server_readonly":false,"server_pointer":true}\n',
            )

        def wait(self, timeout):
            return 0

    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "must-not-leak")
    monkeypatch.setenv("DISPLAY", ":12")
    monkeypatch.setattr("runtime_agent.gameworker.xpra_input.subprocess.Popen", FakeProcess)
    driver = XpraInputDriver(DisplayEnvironment(":99", xauthority="/run/dst/Xauthority"))
    try:
        assert captured["argv"][0] == "/usr/bin/python3"
        assert captured["shell"] is False
        assert captured["env"]["DISPLAY"] == ":99"
        assert captured["env"]["XAUTHORITY"] == "/run/dst/Xauthority"
        assert "CONTROL_PLANE_TOKEN" not in captured["env"]
        process = driver._process
        os.write(process.stdout_write, b'{"ok":true}\n{"ok":true}\n')
        driver.mouse_move(100, 200)
        driver.mouse_down(1)
    finally:
        driver.close()
        os.close(driver._process.stdin_read)
        os.close(driver._process.stdout_write)
