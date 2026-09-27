"""System-Python helper for installed xpra 3.1. Only JSON control uses stdout.

Owns its Unix-only shadow server; EOF, parent death, or error closes the channel.
The installed xpra codec is used instead of maintaining another wire codec.
"""
from __future__ import annotations

import ctypes
import fcntl
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time


class Channel:
    def __init__(self, path):
        from xpra import __version__
        from xpra.net.bencode import bdecode, bencode

        if not __version__.startswith("3.1."):
            raise RuntimeError("xpra version requires protocol revalidation")
        self.encode, self.decode = bencode, bdecode
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(1.5)
        self.socket.connect(path)
        self.send(["hello", {
            "version": __version__, "uuid": f"dst-input-{os.getpid()}",
            "ui_client": True, "windows": False, "keyboard": True,
            "share": False, "wants_aliases": False, "rencode": False,
            "bencode": True, "encodings": ["rgb24"], "network-state": False,
        }])
        self.receive_until(b"hello")

    def send(self, packet):
        data = self.encode(packet)
        self.socket.sendall(struct.pack("!BBBBI", 80, 0, 0, 0, len(data)) + data)

    def read(self, size):
        chunks = bytearray()
        while len(chunks) < size:
            part = self.socket.recv(size - len(chunks))
            if not part:
                raise EOFError("xpra disconnected")
            chunks.extend(part)
        return bytes(chunks)

    def receive_until(self, kind):
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            magic, flags, compression, index, size = struct.unpack("!BBBBI", self.read(8))
            if (magic, flags, compression, index) != (80, 0, 0, 0) or size > 1024 * 1024:
                raise ValueError("unsupported xpra packet")
            packet = self.decode(self.read(size))[0]
            if packet[0] == kind:
                return packet
            if packet[0] in (b"disconnect", b"challenge"):
                raise RuntimeError("xpra channel unavailable")
        raise TimeoutError("xpra response missing")

    def barrier(self):
        # info-request is dispatched on xpra's UI loop after preceding input.
        # This acknowledges server processing, never a gameplay transition.
        self.send(["info-request", [], [], ["server"]])
        self.receive_until(b"info-response")


def parent_death_signal():
    # This bridge has no threads when it forks xpra.
    ctypes.CDLL(None).prctl(1, signal.SIGTERM)


def query(*args):
    return subprocess.run(
        ["xdotool", *args], check=True, capture_output=True, text=True,
        timeout=1, stdin=subprocess.DEVNULL,
    ).stdout.strip()


def focus_game():
    location = dict(line.split("=", 1) for line in query("getmouselocation", "--shell").splitlines())
    window = location["WINDOW"]
    if not window.isdecimal() or query("getwindowname", window) != "Don't Starve Together":
        raise ValueError("anchor is not over DST")
    query("windowfocus", "--sync", window)
    if query("getwindowfocus") != window:
        raise ValueError("DST focus unavailable")


def key_packet(key, pressed):
    # The runtime uses a fixed Xvfb US layout. This is xpra's normal client
    # key-action packet; keycodes are checked against that layout below.
    codes = {"w": 25, "s": 39, "a": 38, "d": 40, "space": 65,
             "tab": 23, "Escape": 9}
    names = {"space": "space", "tab": "Tab", "Escape": "Escape"}
    if key not in codes or type(pressed) is not bool:
        raise ValueError("unsupported key input")
    name = names.get(key, key)
    x11 = ctypes.CDLL("libX11.so.6")
    x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
    x11.XStringToKeysym.restype = ctypes.c_ulong
    keysym = x11.XStringToKeysym(name.encode("ascii"))
    if not keysym:
        raise ValueError("key has no X11 keysym")
    return ["key-action", 1, name, pressed, [], keysym, "", codes[key], 0]


def reply(**values):
    print(json.dumps({"ok": True, **values}), flush=True)


def main():
    display = os.environ.get("DISPLAY", "")
    if not re.fullmatch(r":[0-9]{1,4}(?:\.[0-9]+)?", display):
        raise ValueError("local display required")
    # Refuse a second GameWorker input owner on the same runtime display.
    lock_path = f"/tmp/dst-input-owner-{os.getuid()}-{display[1:]}.lock"
    lock = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    process = None
    channel = None
    buttons, keys = set(), set()
    point = [0, 0]
    def terminate(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    with tempfile.TemporaryDirectory(prefix="dst-input-") as directory:
        try:
            env = os.environ.copy()
            for name in ("XPRA_SYSTEM_CONF_DIRS", "XPRA_USER_CONF_DIRS", "XPRA_DEFAULT_CONF_DIRS"):
                env[name] = directory
            env["XDG_RUNTIME_DIR"] = directory
            # Ignore system/user xpra config: it may contain startup commands,
            # TCP listeners, or clipboard forwarding from an operator's VIEW.
            argv = ["/usr/bin/xpra", "shadow", display, "--daemon=no",
                    f"--socket-dir={directory}", f"--socket-dirs={directory}",
                    f"--bind={directory}/input.sock", "--html=off", "--mdns=no",
                    "--pulseaudio=no", "--speaker=off", "--microphone=off",
                    "--clipboard=no", "--notifications=no", "--dbus-control=no",
                    "--dbus-launch=", "--tray=no", "--exit-with-client=yes",
                    "--server-idle-timeout=15"]
            process = subprocess.Popen(
                argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
                preexec_fn=parent_death_signal,  # noqa: PLW1509 - single-threaded bridge
            )
            path = directory + "/input.sock"
            deadline = time.monotonic() + 8
            while not os.path.exists(path):
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("xpra startup failed")
                time.sleep(.05)
            # The socket can precede the server's accept loop.
            while channel is None:
                try:
                    channel = Channel(path)
                except (ConnectionRefusedError, FileNotFoundError):
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("xpra input handshake failed") from None
                    time.sleep(.05)
            channel.barrier()
            reply(ready=1)
            for line in sys.stdin.buffer:
                if len(line) > 1024:
                    raise ValueError("request too large")
                operation, *args = json.loads(line)
                if operation == "move":
                    x, y = args
                    if not all(type(v) is int and 0 <= v < 4096 for v in (x, y)):
                        raise ValueError("invalid pointer")
                    point = [x, y]
                    channel.send(["pointer-position", 1, point, [], []])
                elif operation == "focus":
                    focus_game()
                elif operation == "button":
                    button, pressed = args
                    if button not in range(1, 6) or type(pressed) is not bool:
                        raise ValueError("invalid button")
                    if pressed:
                        buttons.add(button)
                    channel.send(["button-action", 1, button, pressed, point, [], []])
                    if not pressed:
                        buttons.discard(button)
                elif operation == "key":
                    key, pressed = args
                    packet = key_packet(key, pressed)
                    if pressed:
                        keys.add(key)
                    channel.send(packet)
                    if not pressed:
                        keys.discard(key)
                else:
                    raise ValueError("unsupported input operation")
                channel.barrier()
                reply()
        finally:
            if channel:
                try:
                    for button in buttons:
                        channel.send(["button-action", 1, button, False, point, [], []])
                    for key in keys:
                        channel.send(key_packet(key, False))
                    channel.barrier()
                except Exception as exc:  # noqa: BLE001 - cleanup after channel failure
                    print(f"xpra input release failed: {type(exc).__name__}", file=sys.stderr)
                channel.socket.close()
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            os.close(lock)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - private protocol/process boundary
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "error": type(exc).__name__}), flush=True)
        sys.exit(1)
