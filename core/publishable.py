"""Is this rendered file actually publishable through the Instagram API? A file that merely rendered is not enough.

Limits are taken from Meta's IG User Media reference (checked October 2026):
  feed image   JPEG only, <= 8 MB, aspect ratio 4:5 .. 1.91:1, width 320..1440 px (outside it is rescaled)
  carousel     2..10 items
  Reel / video MP4 or MOV, H.264 or HEVC, AAC audio (mono/stereo, <= 48 kHz), 23..60 fps, 3 s .. 15 min,
               <= 300 MB, width <= 1920 px, aspect ratio 0.01:1 .. 10:1 (9:16 recommended)
The checks run BEFORE upload, inside the render fallback loop, so a file Instagram would refuse makes the pipeline
fall back to a safer format instead of failing after the media was hosted.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VIDEO_BYTES = 300 * 1024 * 1024
IMAGE_RATIO = (0.8, 1.91)              # width / height: 4:5 .. 1.91:1
VIDEO_FPS = (23.0, 60.5)
VIDEO_SECONDS = (3.0, 900.0)
VIDEO_CODECS = {"h264", "hevc"}
AUDIO_CODECS = {"aac"}


class NotPublishable(ValueError):
    """The rendered media would be refused by Instagram. The message says why."""


def check_image(path: Path) -> None:
    p = Path(path)
    if not p.is_file():
        raise NotPublishable(f"{p.name}: file missing")
    if p.stat().st_size > MAX_IMAGE_BYTES:
        raise NotPublishable(f"{p.name}: {p.stat().st_size / 1e6:.1f} MB exceeds the 8 MB image limit")
    try:
        with Image.open(p) as im:
            fmt, (w, h) = im.format, im.size
    except Exception as e:  # noqa: BLE001
        raise NotPublishable(f"{p.name}: not a readable image ({type(e).__name__})") from e
    if fmt != "JPEG":
        raise NotPublishable(f"{p.name}: Instagram feed images must be JPEG (this is {fmt})")
    if w < 320:
        raise NotPublishable(f"{p.name}: width {w}px is below the 320px minimum")
    ratio = w / h
    if not IMAGE_RATIO[0] - 1e-6 <= ratio <= IMAGE_RATIO[1] + 1e-6:
        raise NotPublishable(f"{p.name}: aspect ratio {ratio:.2f} is outside 4:5..1.91:1")


def check_video(path: Path, probe=None) -> dict:
    """Probe the file for real. Returns the probe info when it is acceptable."""
    from .video_renderer import VideoError, probe as _probe
    p = Path(path)
    if not p.is_file():
        raise NotPublishable(f"{p.name}: file missing")
    if p.suffix.lower() not in {".mp4", ".mov"}:
        raise NotPublishable(f"{p.name}: container must be MP4 or MOV")
    if p.stat().st_size > MAX_VIDEO_BYTES:
        raise NotPublishable(f"{p.name}: {p.stat().st_size / 1e6:.0f} MB exceeds the 300 MB video limit")
    try:
        info = (probe or _probe)(p)
    except VideoError as e:
        raise NotPublishable(f"{p.name}: cannot inspect video ({e})") from e
    if info.get("video") not in VIDEO_CODECS:
        raise NotPublishable(f"{p.name}: video codec {info.get('video')} is not H.264/HEVC")
    if info.get("audio") and info["audio"] not in AUDIO_CODECS:
        raise NotPublishable(f"{p.name}: audio codec {info['audio']} is not AAC")
    dur = info.get("duration") or 0
    if not VIDEO_SECONDS[0] <= dur <= VIDEO_SECONDS[1]:
        raise NotPublishable(f"{p.name}: duration {dur:.1f}s is outside 3 s..15 min")
    fps = info.get("fps")
    if fps and not VIDEO_FPS[0] <= fps <= VIDEO_FPS[1]:
        raise NotPublishable(f"{p.name}: {fps:g} fps is outside 23..60")
    size = info.get("size")
    if not size:
        raise NotPublishable(f"{p.name}: no video stream")
    w, h = size
    if w > 1920 or not 0.01 <= w / h <= 10:
        raise NotPublishable(f"{p.name}: {w}x{h} is outside Instagram's resolution/aspect limits")
    return info


def validate(files: list, fmt: str, probe=None) -> None:
    """Raise NotPublishable unless `files` can be published as `fmt` (photo | card | carousel | video | reel)."""
    files = [Path(f) for f in files]
    if fmt in ("video", "reel"):
        if len(files) != 1:
            raise NotPublishable(f"{fmt} needs exactly one video file, got {len(files)}")
        check_video(files[0], probe)
        return
    if fmt in ("photo", "card"):
        if len(files) != 1:
            raise NotPublishable(f"{fmt} needs exactly one image, got {len(files)}")
    elif fmt == "carousel":
        if not 1 <= len(files) <= 10:
            raise NotPublishable(f"a carousel holds 1-10 images, got {len(files)}")
    else:
        raise NotPublishable(f"unknown format '{fmt}'")
    for f in files:
        check_image(f)
