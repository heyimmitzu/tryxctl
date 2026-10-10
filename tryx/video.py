"""Video transcoding for the TRYX display.

The device's player only treats ``.mp4`` files as video — ``setLayout1Path``
checks ``path.endsWith("mp4")`` and hands everything else to the image loader.
So any non-``.mp4`` file (or an ``.mp4`` with an unsupported codec) must be
transcoded to H.264 + AAC in an MP4 container on the host before upload.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v",
              ".mpg", ".mpeg", ".wmv", ".flv", ".ts", ".3gp"}
# Rockchip Android 11 decodes these natively inside an mp4 container.
SAFE_MP4_CODECS = {"h264", "hevc", "h265", "avc"}

_CACHE_DIR = os.path.join(tempfile.gettempdir(), "tryx_videos")


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _ffprobe() -> str | None:
    return shutil.which("ffprobe")


def is_video(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return False
    if ext in VIDEO_EXTS:
        return True
    return _probe_codec_type(path) == "video"


def _probe_codec_type(path: str) -> str:
    ffprobe = _ffprobe()
    if not ffprobe:
        return ""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return out
    except Exception:
        return ""


def _probe_codec(path: str) -> str:
    ffprobe = _ffprobe()
    if not ffprobe:
        return ""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip().lower()
        return out
    except Exception:
        return ""


def needs_transcode(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext != ".mp4":
        # The on-device player requires the ".mp4" suffix.
        return True
    return _probe_codec(path) not in SAFE_MP4_CODECS


def _available_h264_encoder() -> str | None:
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        return None
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None
    # libx264 (best) when present; otherwise libopenh264 (free builds).
    for enc in ("libx264", "libopenh264"):
        if enc in out:
            return enc
    return None


def transcode_to_mp4(input_path: str, max_w: int = 1920,
                     max_h: int = 1080) -> str:
    """Transcode ``input_path`` to a compatible H.264 MP4 (no audio).

    Returns the path to the new file. Raises ``RuntimeError`` on failure.
    """
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg is not installed — required to convert this video. "
            "Install ffmpeg or provide an .mp4 (H.264) file instead.")
    enc = _available_h264_encoder()
    if not enc:
        raise RuntimeError(
            "no H.264 encoder found in ffmpeg (need libx264 or libopenh264). "
            "Install ffmpeg with H.264 support or provide an .mp4 (H.264) file.")

    os.makedirs(_CACHE_DIR, exist_ok=True)
    fd, out_path = tempfile.mkstemp(suffix=".mp4", dir=_CACHE_DIR)
    os.close(fd)

    cmd = [
        ffmpeg, "-y", "-i", input_path,
        "-vf", f"scale={max_w}:{max_h}:force_original_aspect_ratio=decrease"
               ":force_divisible_by=2",
        "-c:v", enc,
    ]
    if enc == "libx264":
        cmd += ["-preset", "veryfast", "-crf", "20"]
    else:  # libopenh264 is bitrate-driven
        cmd += ["-b:v", "4M"]
    cmd += [
        "-pix_fmt", "yuv420p",
        "-an",  # the display has no audio; drop any audio stream
        "-movflags", "+faststart",
        out_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        tail = (proc.stderr or "").strip()[-600:]
        raise RuntimeError(f"ffmpeg failed to convert video: {tail}")
    return out_path


def remote_name_for(input_path: str) -> str:
    """Return the ``.mp4`` name to use on the device for ``input_path``."""
    stem = os.path.splitext(os.path.basename(input_path))[0]
    return f"{stem}.mp4"
