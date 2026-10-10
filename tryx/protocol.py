"""Wire protocol for the TRYX Panorama AIO display (Rockchip 'cm01_se' board).

Reverse-engineered from the device firmware (SerialService / HomeUI APKs).

The host PC talks to the device over a USB CDC-ACM serial port (/dev/ttyACM0)
at 115200 baud. Every message is a UTF-8 "HTTP-like" text document wrapped in a
simple framed + byte-stuffed envelope.

Frame layout (before byte-stuffing)::

    0x5A | len_hi len_lo | content... | crc | 0x5A

- ``len`` is a 2-byte big-endian value equal to ``len(content) + 5``.
- ``crc`` is a single byte: ``(sum of [len_hi, len_lo, content...]) & 0xFF``.
- Byte-stuffing is applied to everything between the 0x5A delimiters:
  0x5A -> 0x5B 0x01, 0x5B -> 0x5B 0x02.

The ``content`` is an HTTP-like document::

    <METHOD> <CMD> <VERSION>\\r\\n
    HeaderKey=Value\\r\\n
    ...\\r\\n
    \\r\\n
    <body>

where METHOD in {GET, POST, STATE, DELETE}, VERSION is "1", and headers carry
DataHeader fields (SeqNumber, AckNumber, ContentLength, ContentType, ...).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

FRAME_DELIM = 0x5A  # b'Z'
ESC = 0x5B  # b'['

# Escape sequences used during byte-stuffing.
STUFF_5A = b"\x5b\x01"
STUFF_5B = b"\x5b\x02"


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------

def stuff(data: bytes) -> bytes:
    """Escape 0x5A and 0x5B bytes per the device's byte-stuffing rules."""
    out = bytearray()
    for b in data:
        if b == FRAME_DELIM:
            out += STUFF_5A
        elif b == ESC:
            out += STUFF_5B
        else:
            out.append(b)
    return bytes(out)


def unstuff(data: bytes) -> bytes:
    """Reverse of :func:`stuff`. Returns the payload with escapes removed."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == ESC and i + 1 < n:
            nxt = data[i + 1]
            if nxt == 0x01:
                out.append(FRAME_DELIM)
                i += 2
                continue
            if nxt == 0x02:
                out.append(ESC)
                i += 2
                continue
        out.append(b)
        i += 1
    return bytes(out)


def build_frame(content: bytes) -> bytes:
    """Wrap ``content`` bytes in a framed, stuffed envelope."""
    length = len(content) + 5
    prefix = length.to_bytes(2, "big")
    crc = sum(prefix) + sum(content)
    crc &= 0xFF
    payload = prefix + content + bytes((crc,))
    return bytes((FRAME_DELIM,)) + stuff(payload) + bytes((FRAME_DELIM,))


def parse_frame(raw: bytes) -> Optional[bytes]:
    """Unwrap a single raw frame (the bytes between 0x5A delimiters).

    Returns the *content* bytes on success, or ``None`` if ``raw`` is not a
    complete, well-formed frame.
    """
    if len(raw) < 2 or raw[0] != FRAME_DELIM or raw[-1] != FRAME_DELIM:
        return None
    body = unstuff(raw[1:-1])
    if len(body) < 3:
        return None
    length = int.from_bytes(body[0:2], "big")
    content = body[2:-1]
    crc = body[-1]
    # The on-device parser ignores the CRC entirely; we validate only lightly.
    expected_crc = (sum(body[:-1])) & 0xFF
    if crc != expected_crc:
        # Be lenient like the device (it discards the CRC without checking).
        pass
    _ = length  # informational only
    return content


def iter_frames(stream_bytes: bytes):
    """Split a raw byte stream into (content, consumed) frames.

    Yields ``content`` for each complete frame found. Handles frames that
    arrive stuck together and ignores partial trailing bytes.
    """
    i = 0
    n = len(stream_bytes)
    while i < n:
        # Look for a start delimiter.
        if stream_bytes[i] != FRAME_DELIM:
            i += 1
            continue
        # Collect until the matching end delimiter.
        buf = bytearray()
        j = i + 1
        while j < n:
            b = stream_bytes[j]
            if b == FRAME_DELIM:
                # End of this frame.
                raw = bytes((FRAME_DELIM,)) + bytes(buf) + bytes((FRAME_DELIM,))
                content = parse_frame(raw)
                if content is not None:
                    yield content
                i = j + 1
                break
            buf.append(b)
            j += 1
        else:
            # No terminator found; wait for more data.
            break


# --------------------------------------------------------------------------
# HTTP-like message (de)serialisation
# --------------------------------------------------------------------------

@dataclass
class Message:
    method: str  # requestState: GET / POST / STATE / DELETE
    cmd: str  # cmdType: conn / transport / config / waterBlockScreenId / ...
    version: str = "1"
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""

    def to_bytes(self) -> bytes:
        lines = [f"{self.method} {self.cmd} {self.version}"]
        for k, v in self.headers.items():
            lines.append(f"{k}={v}")
        text = "\r\n".join(lines) + "\r\n\r\n" + self.body
        return text.encode("utf-8")

    @classmethod
    def from_content(cls, content: bytes) -> "Message":
        text = content.decode("utf-8", "replace")
        head, _, body = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        request = lines[0].split(" ")
        if len(request) >= 3:
            method, cmd, version = request[0], request[1], request[2]
        elif len(request) == 2:
            # Some device replies use "<version> <cmd>" (e.g. "1 200").
            method, cmd, version = "", request[1], request[0]
        else:
            method, cmd, version = "", "", ""
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if "=" in line:
                k, _, v = line.partition("=")
                headers[k] = v
        return cls(method=method, cmd=cmd, version=version,
                   headers=headers, body=body)


def make_request(method: str, cmd: str, body: dict | list | str | None,
                 seq: int, content_type: str = "json") -> bytes:
    """Build a framed request ready to write to the serial port.

    ``body`` may be a dict/list (JSON-encoded), a raw string, or None.
    """
    if body is None:
        body_text = ""
    elif isinstance(body, (dict, list)):
        body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    else:
        body_text = str(body)

    headers = {
        "SeqNumber": str(seq),
        "ContentType": content_type,
    }
    if body_text:
        headers["ContentLength"] = str(len(body_text))

    msg = Message(method=method, cmd=cmd, version="1",
                  headers=headers, body=body_text)
    return build_frame(msg.to_bytes())
