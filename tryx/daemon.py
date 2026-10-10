"""Background daemon that owns the AIO serial connection.

The device reverts its screen to standby ~60 s after the last host message, so
this process sends a lightweight heartbeat (every 8 s) to keep the chosen
image/video on screen even when the GUI/CLI is closed.

It is the *single owner* of the serial port. The GUI and CLI talk to it over a
Unix socket with newline-delimited JSON requests.

Commands (one JSON object per line)::

    {"cmd": "status"}
    {"cmd": "info"}
    {"cmd": "set_image",   "path": "/abs/local.jpg", "ratio": "1:1", "adb": true}
    {"cmd": "set_video",   "path": "...", "ratio": "1:1", "adb": true}
    {"cmd": "set_slideshow", "paths": ["a.jpg","b.jpg"], "ratio": "1:1", "adb": true}
    {"cmd": "set_sysinfo", "items": ["Cpu Usage", ...]}

Each responds with a single JSON object, e.g. ``{"ok": true}``.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time

from .device import TryxDevice, DEFAULT_PORT, DEFAULT_BAUD
from .video import needs_transcode, transcode_to_mp4, remote_name_for, is_video

# 8 s keeps both the device's 60 s connect timeout and its 10 s serial-port
# idle timer from firing, so the connection stays warm without churn.
HEARTBEAT_SECONDS = 8.0
CONNECT_RETRY_SECONDS = 5.0


def socket_path() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(base, f"tryxd-{os.getuid()}.sock")


def _remove_stale_socket(path: str) -> None:
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


class TryxDaemon:
    def __init__(self, port: str = DEFAULT_PORT, baud: int = DEFAULT_BAUD):
        self.port = port
        self.baud = baud
        self.device: TryxDevice | None = None
        self.lock = threading.Lock()
        self.last_config: dict | None = None
        self._stop = threading.Event()

    # -- device lifecycle ---------------------------------------------------

    def _connect(self) -> TryxDevice:
        dev = TryxDevice(self.port, self.baud)
        dev.connect()
        dev.info()  # handshake; raises if the device isn't there
        return dev

    def _ensure_connected(self) -> bool:
        if self.device is not None:
            return True
        try:
            self.device = self._connect()
        except Exception:
            self.device = None
            return False
        # Re-apply the last display state (device may have rebooted and lost
        # its in-memory config, but /sdcard/pcMedia/ persists).
        if self.last_config is not None:
            try:
                self.device.set_screen(self.last_config)
            except Exception:
                pass
        return True

    def _drop_device(self) -> None:
        if self.device is not None:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None

    # -- heartbeat ----------------------------------------------------------

    def _heartbeat(self) -> None:
        with self.lock:
            if not self._ensure_connected():
                return
            try:
                self.device.request("POST", "conn", wait=True, timeout=3.0)
            except Exception:
                self._drop_device()

    def heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            self._heartbeat()
            self._stop.wait(HEARTBEAT_SECONDS)

    # -- command dispatch ---------------------------------------------------

    @staticmethod
    def _prepare_video(path: str) -> tuple[str, str]:
        """Return ``(local_path_to_upload, remote_name)``, transcoding if needed."""
        if needs_transcode(path):
            return transcode_to_mp4(path), remote_name_for(path)
        return path, os.path.basename(path)

    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        # Transcode videos before acquiring the lock, so a long conversion
        # does not starve the heartbeat thread.
        try:
            if cmd == "set_video":
                req = dict(req)
                up, rn = self._prepare_video(req["path"])
                req["path"], req["remote_name"] = up, rn
            elif cmd == "set_slideshow":
                req = dict(req)
                paths, names = [], []
                for p in req["paths"]:
                    if is_video(p):
                        up, rn = self._prepare_video(p)
                    else:
                        up, rn = p, os.path.basename(p)
                    paths.append(up)
                    names.append(rn)
                req["paths"], req["remote_names"] = paths, names
        except Exception as e:
            return {"ok": False, "error": str(e)}

        try:
            with self.lock:
                if not self._ensure_connected():
                    return {"ok": False, "error": "AIO not connected"}

                if cmd == "status":
                    return {"ok": True, "connected": True}

                if cmd == "info":
                    return {"ok": True, "info": self.device.info()}

                adb = bool(req.get("adb", True))

                if cmd == "set_image":
                    name = self.device.upload_file(req["path"], use_adb=adb)
                    self.last_config = self.device.set_image(
                        name, ratio=req.get("ratio", "1:1"))
                    return {"ok": True, "name": name}

                if cmd == "set_video":
                    name = self.device.upload_file(
                        req["path"], remote_name=req.get("remote_name"),
                        use_adb=adb)
                    self.last_config = self.device.set_video(
                        name, ratio=req.get("ratio", "1:1"))
                    return {"ok": True, "name": name}

                if cmd == "set_slideshow":
                    names = [self.device.upload_file(p, remote_name=n, use_adb=adb)
                             for p, n in zip(req["paths"], req["remote_names"])]
                    self.last_config = self.device.set_slideshow(
                        names, ratio=req.get("ratio", "1:1"))
                    return {"ok": True, "names": names}

                if cmd == "set_sysinfo":
                    self.last_config = self.device.set_sysinfo(req.get("items"))
                    return {"ok": True}

                if cmd == "delete_media":
                    self.device.delete_media(include=req.get("include"),
                                             exclude=req.get("exclude"))
                    return {"ok": True}

                return {"ok": False, "error": f"unknown command: {cmd!r}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # -- socket server ------------------------------------------------------

    def _serve_client(self, conn: socket.socket) -> None:
        conn.settimeout(30.0)
        buf = b""
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        req = json.loads(line)
                    except json.JSONDecodeError:
                        resp = {"ok": False, "error": "invalid JSON"}
                    else:
                        resp = self.handle(req)
                    try:
                        conn.sendall(json.dumps(resp).encode() + b"\n")
                    except OSError:
                        return
        except (OSError, socket.timeout):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def serve(self) -> None:
        path = socket_path()
        _remove_stale_socket(path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        os.chmod(path, 0o600)
        srv.listen(16)
        self._stop.clear()
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except OSError:
                    break
                threading.Thread(target=self._serve_client, args=(conn,),
                                 daemon=True).start()
        finally:
            srv.close()
            _remove_stale_socket(path)

    def run(self) -> None:
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        self.serve()

    def stop(self) -> None:
        self._stop.set()
        self._drop_device()


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="TRYX Panorama display daemon")
    p.add_argument("--port", default=DEFAULT_PORT)
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    args = p.parse_args()

    d = TryxDaemon(args.port, args.baud)
    try:
        d.run()
    except KeyboardInterrupt:
        d.stop()


if __name__ == "__main__":
    main()
