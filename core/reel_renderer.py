"""Dynamic Reel composition (what a real Reel looks like, not info cards):

  moving background footage (stock / your clips / animated fallback)   darkened for legibility
  + big word-by-word captions that pop in and highlight the word being spoken
  + hook headline in the first seconds, category tag, handle
  + voiceover (and optional quiet music), short outro card

Pipeline: background segments (ffmpeg) -> concat -> decode frames -> Pillow overlays -> ffmpeg encode.
All geometry respects Instagram's UI-safe zones for 1080x1920.
"""
from __future__ import annotations

import logging
import math
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from . import config
from .feeds import CATEGORY_STYLE
from .image_renderer import clean_for_image, fit_text, hex_rgb, load_font
from .tts import Voiceover
from .video_renderer import VideoError, find_ffmpeg

log = logging.getLogger("reel")

W, H = 1080, 1920
LEAD = 0.35          # silence before the voice starts
OUTRO = 2.4          # seconds of closing card
CAPTION_Y = 1130     # centre line of captions (above the bottom UI, below the middle)
CAPTION_MAX_W = 800  # keeps text clear of the right-hand like/comment buttons
HOOK_WINDOW = (0.25, 3.4)


# ------------------------------------------------------------------ small helpers
def _ffmpeg() -> str:
    exe = find_ffmpeg()
    if not exe:
        raise VideoError("ffmpeg not found - run: pip install imageio-ffmpeg")
    return exe


def _run(cmd: list[str], what: str) -> None:
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    if r.returncode != 0:
        raise VideoError(f"ffmpeg {what} failed: {r.stderr.decode('utf-8', 'replace').strip()[-300:]}")


def _accent(category: str):
    return hex_rgb(CATEGORY_STYLE.get(category, CATEGORY_STYLE["News"])[2])


def gradient_overlay() -> Image.Image:
    """Transparent -> dark ramps at top and bottom so white captions are always readable on any footage."""
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for y in range(H):
        a = 46.0
        if y < 650:
            a += 130.0 * (1 - y / 650.0)
        if y > 850:
            a += 150.0 * ((y - 850) / (H - 850.0)) ** 1.1
        d.line((0, y, W, y), fill=(0, 0, 0, int(min(a, 235))))
    return ov


class _Encoder:
    """ffmpeg process fed with raw RGB frames; stderr goes to a temp file so it can never block."""

    def __init__(self, cmd: list[str]):
        self._err = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=self._err)
        self.broken = False

    def write(self, raw: bytes) -> None:
        if self.broken:
            return
        try:
            self.proc.stdin.write(raw)  # type: ignore[union-attr]
        except (BrokenPipeError, OSError):
            self.broken = True

    def close(self, out_path: Path) -> None:
        try:
            self.proc.stdin.close()  # type: ignore[union-attr]
        except OSError:
            pass
        rc = self.proc.wait(timeout=600)
        if rc != 0 or not out_path.exists() or out_path.stat().st_size < 10_000:
            self._err.seek(0)
            msg = self._err.read().decode("utf-8", "replace").strip()[-300:]
            out_path.unlink(missing_ok=True)
            self._err.close()
            raise VideoError(f"ffmpeg encode failed (code {rc}): {msg}")
        self._err.close()


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


# ------------------------------------------------------------------ backgrounds
def segment_plan(total: float) -> tuple[int, float]:
    n = max(3, min(7, round(total / 4.5)))
    return n, total / n + 0.1


def prepare_footage_background(clips: list[Path], total: float, tmp: Path) -> Path:
    """Cut the clips into equal 9:16 segments (with the dark gradient baked in) and join them."""
    ff = _ffmpeg()
    grad = tmp / "grad.png"
    gradient_overlay().save(grad)
    n, seg = segment_plan(total)
    fps = config.VIDEO_FPS
    segs = []
    for i in range(n):
        clip = clips[i % len(clips)]
        start = (i // len(clips)) * seg
        out = tmp / f"seg_{i:02d}.mp4"
        flt = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,"
               f"fps={fps},tpad=stop_mode=clone:stop_duration=30[b];[b][1:v]overlay=0:0:format=auto,format=yuv420p[o]")
        _run([ff, "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(clip), "-loop", "1", "-i", str(grad),
              "-filter_complex", flt, "-map", "[o]", "-t", f"{seg:.2f}", "-an", "-c:v", "libx264",
              "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-r", str(fps), str(out)], f"segment {i}")
        segs.append(out)
    listing = tmp / "list.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in segs), encoding="utf-8")
    bg = tmp / "bg.mp4"
    _run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(bg)],
         "concat")
    return bg


def prepare_animated_background(category: str, total: float, tmp: Path) -> Path:
    """No footage available: slowly drifting gradient + soft light blobs (still a moving video, not a card)."""
    top, bottom, accent = CATEGORY_STYLE.get(category, CATEGORY_STYLE["News"])
    big_h = H + 500
    mask = Image.linear_gradient("L").resize((W, big_h))
    canvas = Image.composite(Image.new("RGB", (W, big_h), hex_rgb(bottom)),
                             Image.new("RGB", (W, big_h), hex_rgb(top)), mask)
    blobs = []
    for size, col, alpha in ((620, hex_rgb(accent), 60), (760, (255, 255, 255), 26), (520, hex_rgb(bottom), 90)):
        sp = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(sp).ellipse((0, 0, size - 1, size - 1), fill=(*col, alpha))
        blobs.append(sp.filter(ImageFilter.GaussianBlur(size // 7)))
    out = tmp / "bg.mp4"
    fps = config.VIDEO_FPS
    ff = _ffmpeg()
    enc = _Encoder([ff, "-y", "-loglevel", "error", "-nostats", "-f", "rawvideo", "-pix_fmt", "rgb24",
                    "-s", f"{W}x{H}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "21", "-pix_fmt", "yuv420p", str(out)])
    grad = gradient_overlay()
    for f in range(int(total * fps) + 2):
        t = f / fps
        y0 = int(250 + 220 * math.sin(t * 0.35))
        frame = canvas.crop((0, y0, W, y0 + H))
        for k, sp in enumerate(blobs):
            x = int(W / 2 + 380 * math.sin(t * (0.30 + 0.11 * k) + k * 2.1) - sp.width / 2)
            y = int(H / 2 + 620 * math.cos(t * (0.22 + 0.09 * k) + k) - sp.height / 2)
            frame.paste(sp, (x, y), sp)
        frame.paste(grad, (0, 0), grad)
        enc.write(frame.tobytes())
    enc.close(out)
    return out


# ------------------------------------------------------------------ overlays
def chunk_words(words: list[tuple[str, float, float]], max_words: int = 3, max_chars: int = 22) -> list[list[tuple]]:
    chunks: list[list[tuple]] = []
    cur: list[tuple] = []
    for w in words:
        if cur:
            prev = cur[-1][0]
            chars = sum(len(x[0]) + 1 for x in cur) + len(w[0])
            if len(cur) >= max_words or chars > max_chars or prev.endswith((".", "!", "?", ",", ";", ":")):
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


@dataclass
class Chunk:
    start: float
    end: float
    word_starts: list[float]
    states: list[Image.Image]  # one image per word, that word highlighted


def _wrap_words(words: list[str], font, draw, max_w: float) -> list[list[str]]:
    space = draw.textlength(" ", font=font)
    lines, cur, cur_w = [], [], 0.0
    for w in words:
        ww = draw.textlength(w, font=font)
        if cur and cur_w + space + ww > max_w:
            lines.append(cur)
            cur, cur_w = [], 0.0
        cur_w = cur_w + (space if cur else 0.0) + ww
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def _render_chunk(words: list[str], accent, size: int = 112) -> list[Image.Image]:
    """One image per word (that word highlighted). Biggest font that fits in <=2 lines within the safe width;
    very long words shrink the font / add a third line instead of overflowing."""
    probe = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    words = [clean_for_image(w) for w in words] or [""]
    chosen = None
    for sz in range(size, 47, -6):
        font = load_font(sz, True)
        lines = _wrap_words(words, font, probe, CAPTION_MAX_W)
        fits = all(sum(probe.textlength(w, font=font) for w in ln)
                   + probe.textlength(" ", font=font) * (len(ln) - 1) <= CAPTION_MAX_W for ln in lines)
        if fits and len(lines) <= 2:
            chosen = (sz, font, lines)
            break
    if chosen is None:  # extreme case: allow more lines at the smallest size
        font = load_font(48, True)
        chosen = (48, font, _wrap_words(words, font, probe, CAPTION_MAX_W))
    size, font, lines = chosen
    space = probe.textlength(" ", font=font)
    widths = [[probe.textlength(w, font=font) for w in ln] for ln in lines]
    pad, lh = 22, int(size * 1.18)
    w_img = int(max(sum(ws) + space * (len(ws) - 1) for ws in widths)) + pad * 2
    imgs = []
    for hi in range(len(words)):
        img = Image.new("RGBA", (w_img, lh * len(lines) + pad * 2), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        idx = 0
        for li, ln in enumerate(lines):
            lw = sum(widths[li]) + space * (len(ln) - 1)
            x = (w_img - lw) / 2
            for wi, w in enumerate(ln):
                fill = accent if idx == hi else (255, 255, 255)
                d.text((x, pad + li * lh), w, font=font, fill=fill, stroke_width=9, stroke_fill=(8, 8, 16))
                x += widths[li][wi] + space
                idx += 1
        imgs.append(img)
    return imgs


def build_caption_chunks(words: list[tuple[str, float, float]], category: str, total: float) -> list[Chunk]:
    accent = _accent(category)
    out: list[Chunk] = []
    groups = chunk_words(words)
    for gi, g in enumerate(groups):
        start = g[0][1]
        nxt = groups[gi + 1][0][1] if gi + 1 < len(groups) else min(g[-1][2] + 0.25, total)
        out.append(Chunk(start, max(nxt, start + 0.2), [w[1] for w in g], _render_chunk([w[0] for w in g], accent)))
    return out


def _stroked(draw, xy, text, font, fill, stroke=8):
    draw.text(xy, text, font=font, fill=fill, stroke_width=stroke, stroke_fill=(8, 8, 16))


def build_top_bar(cluster: dict) -> Image.Image:
    accent = _accent(cluster["category"])
    img = Image.new("RGBA", (W, 130), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = load_font(32, True)
    x = 96

    def pill(label, fill, fg):
        nonlocal x
        tw = d.textlength(label, font=f)
        d.rounded_rectangle((x, 20, x + tw + 52, 20 + f.size + 26), radius=(f.size + 26) // 2, fill=fill)
        d.text((x + 26, 31), label, font=f, fill=fg)
        x += int(tw + 52 + 16)

    pill(cluster["category"].upper(), accent, (15, 15, 25))
    if cluster.get("breaking"):
        pill("BREAKING", (220, 38, 38), (255, 255, 255))
    handle = clean_for_image(config.ACCOUNT_HANDLE)
    f2 = load_font(30, True)
    _stroked(d, (W - 96 - d.textlength(handle, font=f2), 28), handle, f2, (255, 255, 255), 5)
    return img


def build_hook(hook: str) -> Image.Image:
    img = Image.new("RGBA", (W, 520), (0, 0, 0, 0))   # y 400..920 -> always clear of the captions (centre y=1130)
    d = ImageDraw.Draw(img)
    font, lines, lh = fit_text(d, hook, 834, 500, start=104, min_size=60)
    y = 10
    for ln in lines:
        _stroked(d, (96, y), ln, font, (255, 255, 255), 9)
        y += lh
    return img


def build_outro(cluster: dict) -> Image.Image:
    accent = _accent(cluster["category"])
    img = Image.new("RGBA", (W, H), (0, 0, 0, 178))
    d = ImageDraw.Draw(img)
    big, mid = load_font(72, True), load_font(86, True)
    y = 700
    for text, font, fill in (("Follow for daily updates", big, (255, 255, 255)),
                             (clean_for_image(config.ACCOUNT_HANDLE), mid, accent)):
        x = (W - d.textlength(text, font=font)) / 2
        d.text((x, y), text, font=font, fill=fill)
        y += font.size + 36
    src = "Source: " + ", ".join(cluster["sources"][:3])
    sf, sl, slh = fit_text(d, src, 800, 120, start=34, min_size=24, bold=False)
    for ln in sl:
        d.text(((W - d.textlength(ln, font=sf)) / 2, y + 30), ln, font=sf, fill=(215, 222, 238))
        y += slh
    return img


def _scaled_alpha(img: Image.Image, f: float) -> Image.Image:
    out = img.copy()
    out.putalpha(img.getchannel("A").point(lambda p: int(p * f)))
    return out


# ------------------------------------------------------------------ audio graph
def _audio_args(voice: Voiceover | None, music: Path | None, total: float) -> tuple[list[str], list[str]]:
    """Return (input args, mapping/filter args) for the encoder (video is input 0)."""
    lead_ms = int(LEAD * 1000)
    fade = f"afade=t=out:st={max(total - 1.6, 0):.2f}:d=1.6"
    mv = max(0.02, min(config.MUSIC_VOLUME, 1.0))
    if voice and music:
        inputs = ["-i", str(voice.path), "-stream_loop", "-1", "-i", str(music)]
        flt = (f"[1:a]adelay={lead_ms}:all=1,apad[v];[2:a]volume={mv},{fade}[m];"
               "[v][m]amix=inputs=2:duration=first:dropout_transition=0,volume=2[a]")
        return inputs, ["-filter_complex", flt, "-map", "0:v:0", "-map", "[a]"]
    if voice:
        return ["-i", str(voice.path)], ["-filter_complex", f"[1:a]adelay={lead_ms}:all=1,apad[a]",
                                         "-map", "0:v:0", "-map", "[a]"]
    if music:
        return (["-stream_loop", "-1", "-i", str(music)],
                ["-filter_complex", f"[1:a]volume=0.5,{fade}[a]", "-map", "0:v:0", "-map", "[a]"])
    return (["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"],
            ["-map", "0:v:0", "-map", "1:a:0"])


# ------------------------------------------------------------------ composition
def compose_reel(bg: Path, content: dict, cluster: dict, words: list[tuple[str, float, float]], total: float,
                 voice: Voiceover | None, music: Path | None, out_path: Path) -> Path:
    ff = _ffmpeg()
    fps = config.VIDEO_FPS
    n_frames = int(round(total * fps))
    chunks = build_caption_chunks(words, cluster["category"], total)
    top_bar, hook_img, outro_img = build_top_bar(cluster), build_hook(content["hook"]), build_outro(cluster)
    hook_steps = {k: _scaled_alpha(hook_img, k / 6) for k in range(1, 7)}
    outro_steps = {k: _scaled_alpha(outro_img, k / 6) for k in range(1, 7)}
    outro_start = total - OUTRO

    a_in, a_map = _audio_args(voice, music, total)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    enc = _Encoder([ff, "-y", "-loglevel", "error", "-nostats", "-f", "rawvideo", "-pix_fmt", "rgb24",
                    "-s", f"{W}x{H}", "-r", str(fps), "-i", "-", *a_in, *a_map,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-profile:v", "high",
                    "-pix_fmt", "yuv420p", "-r", str(fps), "-g", str(fps * 2),
                    "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
                    "-movflags", "+faststart", "-shortest", str(out_path)])
    reader = subprocess.Popen([ff, "-v", "error", "-i", str(bg), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame_bytes, last_raw, ci = W * H * 3, b"", 0
    try:
        for f in range(n_frames):
            raw = _read_exact(reader.stdout, frame_bytes)
            if len(raw) < frame_bytes:        # background ended early -> hold the last frame
                raw = last_raw
                if not raw:
                    raise VideoError("background video is empty")
            last_raw = raw
            t = f / fps
            frame = Image.frombytes("RGB", (W, H), raw)
            frame.paste(top_bar, (0, 250), top_bar)

            if HOOK_WINDOW[0] <= t < HOOK_WINDOW[1]:  # fade in / out of the headline
                k = min(6, max(1, int(((t - HOOK_WINDOW[0]) / 0.3) * 6) + 1),
                        max(1, int(((HOOK_WINDOW[1] - t) / 0.3) * 6) + 1))
                frame.paste(hook_steps[k], (0, 400), hook_steps[k])

            while ci + 1 < len(chunks) and t >= chunks[ci + 1].start:
                ci += 1
            if chunks and chunks[ci].start <= t < chunks[ci].end and t < outro_start:
                ch = chunks[ci]
                wi = max(i for i, s in enumerate(ch.word_starts) if s <= t) if ch.word_starts[0] <= t else 0
                img = ch.states[wi]
                age = t - ch.start
                if age < 0.18:  # quick pop-in
                    s = 0.84 + 0.16 * (age / 0.18)
                    img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.BICUBIC)
                frame.paste(img, (int((W - img.width) / 2), int(CAPTION_Y - img.height / 2)), img)

            if t >= outro_start:
                k = min(6, int(((t - outro_start) / 0.4) * 6) + 1)
                frame = Image.alpha_composite(frame.convert("RGBA"), outro_steps[k]).convert("RGB")
            enc.write(frame.tobytes())
    finally:
        reader.stdout.close()  # type: ignore[union-attr]
        reader.kill()
        reader.wait()
    enc.close(out_path)
    return out_path
