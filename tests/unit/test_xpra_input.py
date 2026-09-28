"""Private input bridge framing, environment, and cleanup without a live display."""
import os
import struct

import pytest

from app.runtime.display import DisplayEnvironment
from runtime_agent.gameworker.xpra_bridge import (
    Channel,
    ChannelUnavailable,
    _server_argv,
    _server_input_capabilities,
    _xpra_mouse_log_tail,
    key_packet,
)
from runtime_agent.gameworker.xpra_input import (
    InputError,
    InputTransportError,
    XpraInputDriver,
)


def test_server_disconnect_reason_is_bounded_and_typed():
    assert callable(Channel.barrier)
    channel = Channel.__new__(Channel)
    channel.read = lambda size: struct.pack("!BBBBI", 80, 0, 0, 0, 1) if size == 8 else b"x"
    channel.decode = lambda _data: ([b"disconnect", b"session ended"], None)

    with pytest.raises(ChannelUnavailable, match="session ended"):
        channel.receive_until(b"info-response")


def test_channel_disconnect_is_transport_failure_without_button_retry():
    driver = XpraInputDriver.__new__(XpraInputDriver)
    driver._buffer = bytearray(
        b'{"ok":false,"error":"ChannelUnavailable","detail":"server closed"}\n'
    )

    with pytest.raises(InputTransportError, match="server closed"):
        driver._read(0.1)


def test_console_key_packets_cover_only_validation_command_keys():
    expected = {
        "grave": 49, "Return": 36, "Control_L": 37, "Shift_L": 50,
        "underscore": 20, "parenleft": 18, "parenright": 19,
        "quotedbl": 48,
        "c": 54, "p": 33, "n": 57, "r": 27, "e": 26,
        "h": 43, "l": 46, "b": 56,
    }
    for key, keycode in expected.items():
        assert key_packet(key, True)[7] == keycode

    try:
        key_packet("q", True)
    except ValueError as exc:
        assert str(exc) == "unsupported key input"
    else:
        raise AssertionError("unneeded arbitrary key was accepted")


def test_console_key_packets_propagate_modifier_state():
    shift_down = key_packet("Shift_L", True)
    assert shift_down[4] == []

    underscore = key_packet("underscore", True, {"Shift_L"})
    assert underscore[4] == ["shift"]

    quote = key_packet("quotedbl", True, {"Shift_L"})
    assert quote[4] == ["shift"]

    shift_up = key_packet("Shift_L", False, {"Shift_L"})
    assert shift_up[4] == ["shift"]

    control_down = key_packet("Control_L", True, {"Shift_L"})
    assert control_down[4] == ["shift"]
    both = key_packet("grave", True, {"Control_L", "Shift_L"})
    assert both[4] == ["shift", "control"]
    control_up = key_packet("Control_L", False, {"Control_L"})
    assert control_up[4] == ["control"]


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


def test_broken_xpra_server_channel_reconnects_once_for_pointer_move(monkeypatch):
    processes = []
    replies = [
        (
            b'{"ok":true,"ready":1,"server_readonly":false,"server_pointer":true}\n'
            b'{"ok":false,"error":"BrokenPipeError"}\n'
        ),
        (
            b'{"ok":true,"ready":1,"server_readonly":false,"server_pointer":true}\n'
            b'{"ok":true}\n'
        ),
    ]

    class FakeProcess:
        def __init__(self, _argv, **_kwargs):
            stdin_read, stdin_write = os.pipe()
            stdout_read, stdout_write = os.pipe()
            self.stdin = os.fdopen(stdin_write, "wb", buffering=0)
            self.stdout = os.fdopen(stdout_read, "rb", buffering=0)
            self.stdin_read = stdin_read
            self.stdout_write = stdout_write
            self.returncode = None
            processes.append(self)
            os.write(stdout_write, replies.pop(0))

        def wait(self, timeout):
            self.returncode = 0
            return self.returncode

    monkeypatch.setattr("runtime_agent.gameworker.xpra_input.subprocess.Popen", FakeProcess)
    driver = XpraInputDriver(DisplayEnvironment(":99"))
    try:
        driver.mouse_move(122, 393)
        assert len(processes) == 2
        assert os.read(processes[0].stdin_read, 128) == b'["move", 122, 393]\n'
        assert os.read(processes[1].stdin_read, 128) == b'["move", 122, 393]\n'
    finally:
        driver.close()
        for process in processes:
            os.close(process.stdin_read)
            os.close(process.stdout_write)


def test_broken_xpra_channel_does_not_retry_ambiguous_button_press(monkeypatch):
    processes = []
    replies = [
        (
            b'{"ok":true,"ready":1,"server_readonly":false,"server_pointer":true}\n'
            b'{"ok":true}\n'
            b'{"ok":false,"error":"BrokenPipeError"}\n'
        ),
        (
            b'{"ok":true,"ready":1,"server_readonly":false,"server_pointer":true}\n'
            b'{"ok":true}\n'
            b'{"ok":true}\n'
        ),
    ]

    class FakeProcess:
        def __init__(self, _argv, **_kwargs):
            stdin_read, stdin_write = os.pipe()
            stdout_read, stdout_write = os.pipe()
            self.stdin = os.fdopen(stdin_write, "wb", buffering=0)
            self.stdout = os.fdopen(stdout_read, "rb", buffering=0)
            self.stdin_read = stdin_read
            self.stdout_write = stdout_write
            self.returncode = None
            processes.append(self)
            os.write(
                stdout_write,
                replies.pop(0),
            )

        def wait(self, timeout):
            self.returncode = 0
            return self.returncode

    monkeypatch.setattr("runtime_agent.gameworker.xpra_input.subprocess.Popen", FakeProcess)
    driver = XpraInputDriver(DisplayEnvironment(":99"))
    try:
        driver.mouse_move(122, 393)
        try:
            driver.mouse_down(1)
        except InputError:
            pass
        else:
            raise AssertionError("ambiguous button press unexpectedly succeeded")
        assert len(processes) == 1
        assert os.read(processes[0].stdin_read, 128) == (
            b'["move", 122, 393]\n["button", 1, true]\n'
        )
        driver.mouse_up(1)
        assert len(processes) == 2
        assert os.read(processes[1].stdin_read, 128) == (
            b'["move", 122, 393]\n["button", 1, false]\n'
        )
    finally:
        driver.close()
        for process in processes:
            os.close(process.stdin_read)
            os.close(process.stdout_write)
