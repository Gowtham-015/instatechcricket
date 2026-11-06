"""Original (authorised) video -> narrated Instagram-ready Reel, retaining permitted source audio underneath speech.

Instagram's API publishes video as REELS. Requirements honoured here: MP4 (H.264 video, AAC audio), 9:16 canvas
(1080x1920), 3-90 s, 30 fps, moov atom first (faststart). The source is scaled to fit WITHOUT cropping people out
(a blurred copy of the same footage fills the rest of the canvas), and a small credit strip names the rights holder.
Generated narration is mandatory. Original commentary/event sound is mixed underneath it when the rights file allows
reuse (`keep_audio`); a source without audio receives narration rather than a silent track.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from . import config, reel_studio
from .image_renderer import clean_for_image, load_font
from .provenance import MediaAsset
from .video_renderer import VideoError, find_ffmpeg, probe

log = logging.getLogger("video_post")
CW, CH = 1080, 1920


def credit_strip(text: str, path: Path) -> Path:
    img = Image.new("RGBA", (CW, 96), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, CW, 96), fill=(0, 0, 0, 150))
    f = load_font(30, False)
    t = clean_for_image(text)
    while d.textlength(t, font=f) > CW - 60 and len(t) > 10:
        t = t[:-2]
    d.text((30, 30), t, font=f, fill=(255, 255, 255, 235))
    img.save(path)
    return path


def prepare_original_video(asset: MediaAsset, out_path: Path, *, voiceover: reel_studio.Voiceover,
                           credit_text: str = "") -> Path:
    reel_studio.validate_voiceover(voiceover)
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise VideoError("ffmpeg not found - run: pip install imageio-ffmpeg")
    src = Path(asset.path)
    info = probe(src)
    dur = min(float(info.get("duration") or asset.duration or 0), config.VIDEO_MAX_SECONDS)
    if dur < config.VIDEO_MIN_SECONDS:
        raise VideoError("video too short for a Reel")
    keep_audio = bool(info.get("audio")) and asset.has_audio
    out_path = Path(out_path)
    base_path = out_path.with_name(f"{out_path.stem}_base.mp4")
    strip = credit_strip(credit_text or f"Video: {asset.attribution or asset.creator or asset.source}",
                         out_path.with_suffix(".credit.png"))
    fc = (f"[0:v]split=2[a][b];[a]scale={CW}:{CH}:force_original_aspect_ratio=increase,crop={CW}:{CH},boxblur=30:5[bg];"
          f"[b]scale={CW}:{CH}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[base];"
          f"[base][1:v]overlay=0:{CH - 330}:format=auto,fps={config.VIDEO_FPS},format=yuv420p[v]")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-t", f"{dur:.2f}", "-i", str(src), "-i", str(strip)]
    if not keep_audio:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100:d={dur:.2f}"]
    cmd += ["-filter_complex", fc, "-map", "[v]"]
    if keep_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
    else:
        cmd += ["-map", "2:a:0", "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-profile:v", "high", "-movflags", "+faststart",
            "-r", str(config.VIDEO_FPS), str(base_path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    strip.unlink(missing_ok=True)
    if r.returncode != 0 or not base_path.exists():
        base_path.unlink(missing_ok=True)
        raise VideoError(f"ffmpeg failed: {r.stderr.strip()[-300:]}")
    out = probe(base_path)
    if out.get("size") != (CW, CH) or out.get("video") != "h264":
        base_path.unlink(missing_ok=True)
        raise VideoError(f"encoded video is not Reel-compatible ({out.get('video')} {out.get('size')})")
    try:
        reel_studio.mux_voiceover(base_path, voiceover, out_path)
    finally:
        base_path.unlink(missing_ok=True)
    log.info("Prepared original video %s (%.1fs, audio=%s)", out_path.name, dur, keep_audio)
    return out_path
