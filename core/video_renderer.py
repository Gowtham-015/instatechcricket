"""Turn the rendered 9:16 cards into an Instagram-compliant Reel (MP4, H.264 + AAC, yuv420p, faststart).

Animation: slow zoom on every card + a clean push-slide transition between cards. Frames are produced with Pillow and
streamed into ffmpeg, so there is nothing to install except `pip install imageio-ffmpeg`
(it bundles an ffmpeg binary for Windows/macOS/Linux). A system ffmpeg on PATH is used first if present.

Audio: Instagram expects an audio track, so a silent AAC track is added. Set MUSIC_PATH to a
ROYALTY-FREE file you have rights to use and it is looped, faded out and mixed in instead.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from . import config

log = logging.getLogger("video")
MIN_SECONDS = 5.0


class VideoError(RuntimeError):
    pass


def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - package missing or binary unavailable
        return None


def encoder_available() -> bool:
    return find_ffmpeg() is not None


def _zoomed(img: Image.Image, t: float, zoom: float) -> Image.Image:
    """Centre-crop for a slow push-in; t in [0,1]. Uses resize(box=...) so it is a single fast resample."""
    w, h = img.size
    z = 1.0 + zoom * t
    cw, ch = w / z, h / z
    x0, y0 = (w - cw) / 2, (h - ch) / 2
    return img.resize((w, h), Image.BILINEAR, box=(x0, y0, x0 + cw, y0 + ch))


def _push(cur: Image.Image, nxt: Image.Image, e: float) -> Image.Image:
    """Slide transition: current card is pushed out to the left while the next one enters from the right.
    (A crossfade of two text cards looks like overlapping ghost text, a push stays crisp.)"""
    w, h = cur.size
    off = int(round(w * e))
    canvas = Image.new("RGB", (w, h))
    canvas.paste(cur, (-off, 0))
    canvas.paste(nxt, (w - off, 0))
    return canvas


def _frame_sequence(slides: list[Image.Image], seconds: list[float], fps: int, fade_s: float, zoom: float):
    """Yield raw RGB frames: per-slide zoom with push transitions between consecutive slides."""
    fade = max(1, int(round(fade_s * fps)))
    counts = [max(int(round(s * fps)), fade * 2 + 1) for s in seconds]
    last = len(slides) - 1
    for i, img in enumerate(slides):
        n = counts[i]
        start = fade if i > 0 else 0  # first `fade` frames were already shown as the crossfade
        for k in range(start, n):
            frame = _zoomed(img, k / max(n - 1, 1), zoom)
            if i < last and k >= n - fade:
                j = k - (n - fade)  # 0..fade-1 -> position inside next slide's opening
                nxt = _zoomed(slides[i + 1], j / max(counts[i + 1] - 1, 1), zoom)
                p = (j + 1) / (fade + 1)
                frame = _push(frame, nxt, p * p * (3 - 2 * p))  # smoothstep easing
            yield frame.convert("RGB").tobytes()


def encode_slideshow(slides: list[Image.Image], out_path: Path, *, seconds_per_slide: float | None = None,
                     fps: int | None = None, fade_s: float = 0.45, zoom: float = 0.05,
                     music_path: str | None = None) -> Path:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise VideoError("ffmpeg not found - run: pip install imageio-ffmpeg")
    if len(slides) < 2:
        raise VideoError("need at least 2 slides")
    fps = fps or config.VIDEO_FPS
    per = seconds_per_slide or config.VIDEO_SECONDS_PER_SLIDE
    w, h = slides[0].size
    n = len(slides)

    def plan(p: float) -> tuple[list[float], float]:
        secs = [p * 0.8] + [p] * (n - 2) + [p * 1.1]  # snappy cover, a little longer CTA
        return secs, sum(max(x, fade_s * 2 + 1 / fps) for x in secs) - fade_s * (n - 1)

    seconds, total_s = plan(per)
    while total_s < MIN_SECONDS:  # Instagram needs >=3 s (Reels tab 5 s+): never emit an invalid clip
        per *= 1.25
        seconds, total_s = plan(per)
    if (w, h) != (1080, 1920):
        log.warning("Reel frames are %dx%d (Instagram recommends 1080x1920)", w, h)
    if total_s > 85:
        raise VideoError(f"video would be {total_s:.0f}s; API Reels must stay under 90s")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [ffmpeg, "-y", "-loglevel", "error", "-nostats",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-"]
    music = Path(music_path) if music_path else None
    if music and music.exists():
        cmd += ["-stream_loop", "-1", "-i", str(music),
                "-af", f"afade=t=out:st={max(total_s - 1.5, 0):.2f}:d=1.5,volume=0.6"]
    else:
        if music_path:
            log.warning("MUSIC_PATH '%s' not found - using a silent audio track", music_path)
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(fps), "-g", str(fps * 2),
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart", "-shortest", str(out_path)]

    with tempfile.TemporaryFile() as err:  # file (not PIPE) -> ffmpeg can never block on a full stderr buffer
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err)
        try:
            assert proc.stdin is not None
            for raw in _frame_sequence(slides, seconds, fps, fade_s, zoom):
                proc.stdin.write(raw)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # ffmpeg died early; the real reason is read below
        finally:
            rc = proc.wait(timeout=300)
        if rc != 0 or not out_path.exists() or out_path.stat().st_size < 10_000:
            err.seek(0)
            msg = err.read().decode("utf-8", "replace").strip()[-400:]
            out_path.unlink(missing_ok=True)
            raise VideoError(f"ffmpeg failed (code {rc}): {msg}")
    log.info("Encoded Reel %s (%.1fs, %.1f MB)", out_path.name, total_s, out_path.stat().st_size / 1e6)
    return out_path


def probe(path: Path) -> dict:
    """Read basic stream info by parsing `ffmpeg -i` (ffprobe is not bundled with imageio-ffmpeg)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise VideoError("ffmpeg not found")
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    text = r.stderr
    info: dict = {"video": None, "audio": None, "size": None, "duration": None, "fps": None}
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
    if m:
        info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = re.search(r"Video: (\w+).*?, (\d{2,5})x(\d{2,5})", text)
    if m:
        info["video"], info["size"] = m[1], (int(m[2]), int(m[3]))
    m = re.search(r"(\d+(?:\.\d+)?) fps", text)
    if m:
        info["fps"] = float(m[1])
    m = re.search(r"Audio: (\w+)", text)
    if m:
        info["audio"] = m[1]
    return info
