"""High-level client for the TRYX Panorama AIO display.

Provides a :class:`TryxDevice` that speaks the device's serial protocol:
handshake, media upload, and display-mode switching.

Display-selection semantics (reverse-engineered from HomeUI ``MainActivity``
``handleMessage``, MSG_REFRESH_CONFIG == 105):

* A ``ScreenConfig`` with ``id == "Customization"`` shows user media.
  - ``screenMode == "Full Screen"`` plays the ``media`` list (one layout).
  - ``screenMode == "Screen Splitting"`` shows two media/layouts side by side.
* ``playMode``: ``"Single"`` (show media[0], loop), ``"Loop"`` (cycle through
  the list), ``"Shuffle"`` (random).
* ``media`` entries are *relative file names* under ``sdcard/pcMedia/`` (the
  app prepends ``basePath = "sdcard/pcMedia/"``).
* ``ratio``: ``""`` (native) | ``"1:1"`` | ``"3:2"`` | ``"4:3"`` | ``"16:9"``.
* Any other ``id`` (e.g. ``"Preset:Rain"``) plays a bundled theme video from
  ``/system/media/video/<Name>.mp4``.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
import time
from typing import Optional

import serial

from . import protocol

DEFAULT_PORT = "/dev/ttyACM0"
DEFAULT_BAUD = 115200

# Device-side media directory (see SerialService: path = "sdcard/pcMedia/").
# No leading slash in the firmware, but the absolute form works for adb push.
MEDIA_DIR_REL = "sdcard/pcMedia"
MEDIA_DIR_ABS = "/sdcard/pcMedia"

VALID_RATIOS = ("", "1:1", "3:2", "4:3", "16:9")

# USB identity of the AIO display board.
TRYX_VID = "18d1"
TRYX_PID = "2d04"


def detect_device() -> dict:
    """Check whether the TRYX display is present on USB.

    Returns a small dict describing the connection state (used by `scan` and
    for clearer error messages).
    """
    import subprocess

    result = {
        "present": False,
        "lsusb": None,
        "serial_ports": [],
        "adb_devices": [],
    }

    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True,
                             timeout=10).stdout
        for line in out.splitlines():
            if f"{TRYX_VID}:{TRYX_PID}" in line:
                result["present"] = True
                result["lsusb"] = line.strip()
                break
    except Exception:
        pass

    result["serial_ports"] = sorted(
        glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))

    try:
        out = subprocess.run(["adb", "devices"], capture_output=True,
                             text=True, timeout=10).stdout
        result["adb_devices"] = [ln.split() for ln in out.splitlines()[1:]
                                 if ln.strip() and "devices" not in ln]
    except Exception:
        pass

    return result


class TryxError(Exception):
    """Raised when the device rejects or fails a request."""


class TryxDevice:
    def __init__(self, port: str = DEFAULT_PORT, baud: int = DEFAULT_BAUD,
                 timeout: float = 1.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser: Optional[serial.Serial] = None
        self._seq = 0
        self._rxbuf = bytearray()

    # -- connection ---------------------------------------------------------

    def connect(self) -> None:
        self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        self._ser.reset_input_buffer()
        self._rxbuf = bytearray()

    def close(self) -> None:
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    def __enter__(self) -> "TryxDevice":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- low level ----------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def send_frame(self, frame: bytes) -> None:
        assert self._ser is not None, "not connected"
        self._ser.write(frame)
        self._ser.flush()

    def read_frame(self, timeout: float | None = None) -> protocol.Message:
        """Read one complete frame and return the parsed Message.

        Raises ``TimeoutError`` if no complete frame arrives within ``timeout``
        (defaults to the serial port timeout).
        """
        assert self._ser is not None, "not connected"
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for a frame")
            data = self._ser.read(self._ser.in_waiting or 1)
            if data:
                self._rxbuf += data
            content, consumed = self._extract_frame(bytes(self._rxbuf))
            if content is not None:
                del self._rxbuf[:consumed]
                return protocol.Message.from_content(content)
            if consumed is not None:
                del self._rxbuf[:consumed]

    @staticmethod
    def _extract_frame(buf: bytes) -> tuple[Optional[bytes], Optional[int]]:
        """Extract the first complete frame from ``buf``.

        Returns ``(content, consumed)``; ``content`` is the unstuffed payload
        of a complete frame (or None if none complete yet), ``consumed`` is the
        number of bytes to drop from the buffer.
        """
        if not buf:
            return None, None
        start = buf.find(bytes((protocol.FRAME_DELIM,)))
        if start < 0:
            return None, len(buf)
        if start > 0:
            return None, start
        i = 1
        while i < len(buf):
            b = buf[i]
            if b == protocol.FRAME_DELIM:
                raw = buf[0 : i + 1]
                return protocol.parse_frame(raw), i + 1
            if b == protocol.ESC:
                i += 2
            else:
                i += 1
        return None, None

    def request(self, method: str, cmd: str,
                body: dict | list | str | None = None,
                wait: bool = True,
                timeout: float = 5.0) -> Optional[protocol.Message]:
        """Send a request and (optionally) wait for the device's reply."""
        seq = self._next_seq()
        frame = protocol.make_request(method, cmd, body, seq)
        self.send_frame(frame)
        if not wait:
            return None
        return self.read_frame(timeout=timeout)

    # -- high level ---------------------------------------------------------

    def handshake(self) -> dict:
        """Send ``POST conn`` and return the parsed device-info JSON."""
        reply = self.request("POST", "conn", wait=True)
        if reply is None:
            raise TryxError("no response to handshake")
        if reply.body:
            try:
                return json.loads(reply.body)
            except json.JSONDecodeError:
                pass
        return {}

    def info(self) -> dict:
        return self.handshake()

    # -- media upload -------------------------------------------------------

    @staticmethod
    def _adb_available() -> bool:
        try:
            subprocess.run(["adb", "version"], capture_output=True,
                           timeout=5, check=False)
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def upload_file(self, local_path: str, remote_name: Optional[str] = None,
                    use_adb: bool = True) -> str:
        """Upload ``local_path`` to the device media dir.

        Returns the *relative* file name to put in a ScreenConfig ``media``
        list (the device prepends ``sdcard/pcMedia/``).
        """
        if not os.path.isfile(local_path):
            raise TryxError(f"file not found: {local_path}")
        remote_name = remote_name or os.path.basename(local_path)

        if use_adb and self._adb_available():
            self._adb_push(local_path, f"{MEDIA_DIR_ABS}/{remote_name}")
        else:
            self._serial_upload(local_path, remote_name)
        return remote_name

    def _adb_push(self, local_path: str, remote_path: str) -> None:
        proc = subprocess.run(["adb", "push", local_path, remote_path],
                              capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise TryxError(f"adb push failed: {proc.stderr.strip()}")

    def _serial_upload(self, local_path: str, remote_name: str) -> None:
        size = os.path.getsize(local_path)
        with open(local_path, "rb") as fh:
            md5 = hashlib.md5(fh.read()).hexdigest()

        reply = self.request(
            "POST", "transport",
            {"fileName": remote_name, "fileSize": size, "type": "media"},
            wait=True, timeout=10,
        )
        if reply is None:
            raise TryxError("no response to transport request")

        with open(local_path, "rb") as fh:
            while True:
                chunk = fh.read(4096)
                if not chunk:
                    break
                assert self._ser is not None
                self._ser.write(chunk)
                self._ser.flush()
                time.sleep(0.02)

        reply = self.request(
            "POST", "transported",
            {"fileName": remote_name, "md5": md5},
            wait=True, timeout=10,
        )
        if reply is None:
            raise TryxError("no response to transported request")

    # -- display control ----------------------------------------------------

    def set_screen(self, config: dict) -> dict:
        """Push a ScreenConfig via ``waterBlockScreenId``; return it."""
        self.request("POST", "waterBlockScreenId", config, wait=True)
        return config

    def set_image(self, remote_name: str, ratio: str = "1:1") -> dict:
        return self.set_screen(self.make_media_config([remote_name], "Single", ratio))

    def set_video(self, remote_name: str, ratio: str = "1:1") -> dict:
        return self.set_screen(self.make_media_config([remote_name], "Single", ratio))

    def set_slideshow(self, remote_names: list[str], ratio: str = "1:1") -> dict:
        return self.set_screen(self.make_media_config(remote_names, "Loop", ratio))

    def set_sysinfo(self, items: Optional[list[str]] = None) -> None:
        """Show the hardware-monitoring overlay (no custom media)."""
        items = items or ["Cpu Usage", "Gpu Usage", "Date&Time"]
        cfg = {
            "Type": "sysinfo",
            "id": "Customization",
            "screenMode": "Full Screen",
            "ratio": "1:1",
            "playMode": "Single",
            "media": [],
            "sysinfoDisplay": items,
            "settings": self._default_settings(),
        }
        return self.set_screen(cfg)

    # -- ScreenConfig builders ---------------------------------------------

    def make_media_config(self, media: list[str], play_mode: str,
                          ratio: str) -> dict:
        if ratio not in VALID_RATIOS:
            raise TryxError(f"invalid ratio {ratio!r}; use one of {VALID_RATIOS}")
        return {
            "Type": "image" if not any(m.lower().endswith(".mp4")
                                       for m in media) else "media",
            "id": "Customization",
            "screenMode": "Full Screen",
            "ratio": ratio,
            "playMode": play_mode,
            "media": media,
            "sysinfoDisplay": [],
            "settings": self._default_settings(),
        }

    @staticmethod
    def _default_settings() -> dict:
        # A valid PmSetting: badges must be a list (showName does addAll()).
        return {
            "align": "Center",
            "color": "#FFFFFF",
            "badges": [],
            "position": "Top",
            "filter": {"value": "", "opacity": 0},
        }

    def delete_media(self, include: list[str] | None = None,
                     exclude: list[str] | None = None) -> None:
        """Delete files from the device media directory."""
        if include is not None:
            self.request("POST", "mediaDelete", {"include": include}, wait=True)
        elif exclude is not None:
            self.request("POST", "mediaDelete", {"exclude": exclude}, wait=True)
        else:
            self.request("POST", "mediaDelete", {"exclude": []}, wait=True)
