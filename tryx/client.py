"""Client for the TRYX display daemon.

The GUI and CLI use this instead of talking to the serial port directly, so a
single background daemon owns the connection and keeps the display alive after
the front-end exits.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

from .device import DEFAULT_PORT, DEFAULT_BAUD
from .daemon import socket_path


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _daemon_alive() -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(socket_path())
        s.sendall(b'{"cmd":"status"}\n')
        data = s.recv(4096)
        s.close()
        return b'"ok"' in data or b"true" in data
    except OSError:
        return False


def ensure_daemon(port: str = DEFAULT_PORT, baud: int = DEFAULT_BAUD,
                  wait: float = 8.0) -> None:
    """Start the daemon if it isn't already running, then wait for its socket."""
    if _daemon_alive():
        return

    subprocess.Popen(
        [sys.executable, "-m", "tryx.daemon", "--port", port, "--baud", str(baud)],
        cwd=_project_root(),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if _daemon_alive():
            return
        time.sleep(0.15)
    # Give up silently; the client will surface a clearer error on use.


class TryxClient:
    """Thin RPC client with a device-like interface (local file paths)."""

    def __init__(self, port: str = DEFAULT_PORT, baud: int = DEFAULT_BAUD):
        ensure_daemon(port, baud)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Video transcoding + upload can take a while for large files.
        self.sock.settimeout(3600.0)
        self.sock.connect(socket_path())

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "TryxClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _call(self, req: dict) -> dict:
        self.sock.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        try:
            resp = json.loads(buf.decode())
        except json.JSONDecodeError:
            return {"ok": False, "error": f"bad daemon response: {buf!r}"}
        return resp

    # -- device-like interface ---------------------------------------------

    def status(self) -> bool:
        r = self._call({"cmd": "status"})
        return bool(r.get("ok"))

    def info(self) -> dict:
        r = self._call({"cmd": "info"})
        if not r.get("ok"):
            raise ConnectionError(r.get("error", "daemon error"))
        return r.get("info", {})

    def set_image(self, local_path: str, ratio: str = "1:1",
                  use_adb: bool = True) -> dict:
        return self._call({"cmd": "set_image", "path": local_path,
                           "ratio": ratio, "adb": use_adb})

    def set_video(self, local_path: str, ratio: str = "1:1",
                  use_adb: bool = True) -> dict:
        return self._call({"cmd": "set_video", "path": local_path,
                           "ratio": ratio, "adb": use_adb})

    def set_slideshow(self, local_paths: list[str], ratio: str = "1:1",
                      use_adb: bool = True) -> dict:
        return self._call({"cmd": "set_slideshow", "paths": list(local_paths),
                           "ratio": ratio, "adb": use_adb})

    def set_sysinfo(self, items: list[str] | None = None) -> dict:
        return self._call({"cmd": "set_sysinfo", "items": items})

    def delete_media(self, include: list[str] | None = None,
                     exclude: list[str] | None = None) -> dict:
        return self._call({"cmd": "delete_media", "include": include,
                           "exclude": exclude})
