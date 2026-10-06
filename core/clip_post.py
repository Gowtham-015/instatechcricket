"""Publish a clip YOU supply (official footage you have permission/licence to use) as a Reel.

The agent does everything around the clip: reformats it to 9:16 (blurred fill so nothing is cropped away),
keeps the ORIGINAL audio (crowd, commentary, celebrations), adds a hook headline and a visible credit line,
writes a fact-grounded caption from real news, and publishes it.

It never downloads other people's videos. Rights are the operator's responsibility: --credit and
--confirm-rights are mandatory so the source is always named on screen and in the caption.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

from . import config
from .ai_writer import compose_caption
from .dedupe import jaccard, tokens
from .image_renderer import clean_for_image, fit_text
from .media_host import MediaHostError
from .ranker import build_clusters, refine_category
from .reel_renderer import H, W, build_hook, build_top_bar
from .storage import LockError, ProcessLock
from .video_renderer import VideoError, find_ffmpeg, probe

log = logging.getLogger("clip_post")
MAX_SECONDS = 85.0


class ClipError(RuntimeError):
    pass


# ------------------------------------------------------------------ story context
def synthetic_cluster(topic: str, notes: str, credit: str, category: str | None) -> dict:
    cat = category or refine_category("News", topic, notes)
    art = {"id": hashlib.sha1(topic.encode()).hexdigest()[:12], "title": topic, "summary": notes or topic,
           "url": "", "source": credit, "category": cat, "published": datetime.now(timezone.utc).isoformat()}
    return {"fingerprint": hashlib.sha1(topic.lower().encode()).hexdigest()[:14], "category": cat,
            "articles": [art], "lead": art, "sources": [credit], "breaking": False, "score": 100.0, "age_hours": 0.0}


def match_news_cluster(topic: str, articles: list[dict], threshold: float = 0.22) -> dict | None:
    """Find the real news story that matches the topic so the caption is grounded in reporting."""
    want = tokens(topic)
    best, best_s = None, 0.0
    for c in build_clusters(articles):
        have = tokens(c["lead"]["title"] + " " + c["lead"]["summary"][:200])
        score = jaccard(want, have)
        if len(want & have) >= 2 and score > best_s:
            best, best_s = c, score
    return best if best_s >= threshold else None


# ------------------------------------------------------------------ video preparation
def _credit_bar(credit: str) -> Image.Image:
    img = Image.new("RGBA", (W, 120), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f, lines, lh = fit_text(d, "Video: " + clean_for_image(credit), 760, 100, start=34, min_size=24, bold=False)
    y = 10
    for ln in lines[:2]:
        d.text((96, y), ln, font=f, fill=(255, 255, 255), stroke_width=5, stroke_fill=(8, 8, 16))
        y += lh
    return img


def _overlays(cluster: dict, hook: str | None, credit: str, tmp: Path) -> tuple[Path, Path]:
    static = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bar = build_top_bar(cluster)
    static.paste(bar, (0, 250), bar)
    cb = _credit_bar(credit)
    static.paste(cb, (0, 1290), cb)
    hook_full = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if hook:
        h = build_hook(hook)
        hook_full.paste(h, (0, 400), h)
    s, k = tmp / "static.png", tmp / "hook.png"
    static.save(s)
    hook_full.save(k)
    return s, k


def prepare_clip(src: Path, out: Path, *, cluster: dict, hook: str | None, credit: str,
                 max_seconds: float = MAX_SECONDS, fps: int | None = None) -> Path:
    ff = find_ffmpeg()
    if not ff:
        raise VideoError("ffmpeg not found - run: pip install imageio-ffmpeg")
    src = Path(src)
    if not src.is_file():
        raise ClipError(f"video not found: {src}")
    info = probe(src)
    if not info.get("video"):
        raise ClipError("that file does not contain a readable video stream")
    dur = min(info.get("duration") or max_seconds, max_seconds)
    if dur < 3.0:
        raise ClipError("clip is shorter than 3 seconds (Instagram's minimum for Reels)")
    fps = fps or config.VIDEO_FPS
    tmp = Path(tempfile.mkdtemp(prefix="clip_"))
    try:
        static_png, hook_png = _overlays(cluster, hook, credit, tmp)
        flt = ("[0:v]split[a][b];"
               f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=40:6,eq=brightness=-0.10[bg];"
               f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
               f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={fps},setsar=1[v0];"
               "[v0][1:v]overlay=0:0:format=auto[v1];"
               "[v1][2:v]overlay=0:0:enable='between(t,0.25,3.4)':format=auto,format=yuv420p[v]")
        cmd = [ff, "-y", "-loglevel", "error", "-i", str(src), "-loop", "1", "-i", str(static_png),
               "-loop", "1", "-i", str(hook_png)]
        if info.get("audio"):
            amap = ["-map", "0:a:0"]
        else:  # Instagram expects an audio track
            cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
            amap = ["-map", "3:a:0"]
        cmd += ["-filter_complex", flt, "-map", "[v]", *amap, "-t", f"{dur:.2f}", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "21", "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(fps),
                "-g", str(fps * 2), "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
                "-movflags", "+faststart", "-shortest", str(out)]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(cmd, capture_output=True, timeout=900)
        if r.returncode != 0 or not Path(out).exists() or Path(out).stat().st_size < 10_000:
            raise VideoError("ffmpeg failed: " + r.stderr.decode("utf-8", "replace").strip()[-300:])
        return Path(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ publish
def post_clip(pipeline, *, video: Path, topic: str, credit: str, notes: str = "", category: str | None = None,
              dry: bool = False, from_news: bool = True) -> dict:
    """`pipeline` supplies the writer, media host, Instagram client, history and health (same as scheduled posts)."""
    topic, credit = topic.strip(), credit.strip()
    if len(topic) < 10:
        raise ClipError("--topic should be a clear one-line description of the story")
    if not credit:
        raise ClipError("--credit is required (who owns the footage, e.g. 'BCCI / Star Sports')")
    now = pipeline.now()
    try:
        lock = ProcessLock(pipeline.data_dir / "agent.lock")
        lock.acquire()
    except LockError as e:
        return {"status": "skipped", "reason": str(e)}
    tmp = Path(tempfile.mkdtemp(prefix="clippost_"))
    try:
        cluster = None
        if from_news:
            try:
                cluster = match_news_cluster(topic, pipeline.collector())
            except Exception as e:  # noqa: BLE001 - news lookup is optional
                log.info("News lookup skipped (%s)", type(e).__name__)
        if cluster:
            log.info("Matched real news story: %s", cluster["lead"]["title"][:80])
        else:
            cluster = synthetic_cluster(topic, notes, credit, category)
        if category:
            cluster["category"] = category
        content, mode = pipeline.writer.write(cluster)
        caption = compose_caption(content, cluster, credit)
        stem = f"{now.strftime('%Y%m%d%H%M%S')}_clip_{cluster['fingerprint'][:6]}"
        mp4 = prepare_clip(video, tmp / f"{stem}.mp4", cluster=cluster, hook=content["hook"], credit=credit)
        record = {"id": stem, "title": cluster["lead"]["title"], "url": cluster["lead"].get("url", ""),
                  "category": cluster["category"], "fingerprint": cluster["fingerprint"], "sources": cluster["sources"],
                  "score": cluster["score"], "breaking": False, "mode": mode, "slot": None, "format": "reel",
                  "reel": f"own clip, credit={credit}", "caption_len": len(caption), "published_at": now.isoformat(),
                  "articles": [{"url": a["url"], "title": a["title"]} for a in cluster["articles"][:6]]}
        if dry:
            out = config.PREVIEW_DIR
            out.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(mp4, out / mp4.name)
            (out / f"{stem}_caption.txt").write_text(caption, encoding="utf-8")
            return {"status": "dry_run", "format": "reel", "title": record["title"], "files": [str(out / mp4.name)]}
        hosted = []
        try:
            hosted.append(pipeline.host.upload(mp4, mp4.name))
            if not pipeline._reachable(hosted[0].url, "video"):
                raise MediaHostError("hosted video not publicly reachable")
            result = pipeline.ig.publish_reel(hosted[0].url, caption)
        finally:
            for h in hosted:
                try:
                    pipeline.host.delete(h.ref)
                except Exception:  # noqa: BLE001
                    log.info("Could not delete hosted media (will be cleaned up later)")
        record.update(media_id=result.media_id, permalink=result.permalink)
        history = pipeline.load_history()
        history.append(record)
        pipeline.save_history(history)
        pipeline.health.record_success()
        log.info("Published own clip | %s | %s", record["title"][:70], result.permalink)
        return {"status": "published", "format": "reel", "title": record["title"], "permalink": result.permalink}
    finally:
        lock.release()
        shutil.rmtree(tmp, ignore_errors=True)
