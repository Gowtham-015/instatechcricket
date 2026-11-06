"""Orchestrates one dynamic Reel: narration -> required voiceover -> footage -> render.
Footage failures degrade gracefully; voice failures reject the Reel so the pipeline can choose a still format."""
from __future__ import annotations

import inspect
import logging
import math
import random
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config
from .commons import CommonsClient, context_terms, images_credit, photo_queries_for
from .localphotos import LocalPhotos
from .openverse import OpenverseClient
from .reel_renderer import (LEAD, OUTRO, compose_reel, prepare_animated_background, prepare_footage_background,
                            prepare_mixed_background, segment_plan)
from .stock import configured_providers, local_clips, queries_for
from .tts import Voiceover, synthesize
from .variety import ReelStyle
from .video_renderer import VideoError, find_ffmpeg, probe

log = logging.getLogger("reel_studio")
CTA_SENTENCE = "Follow for more updates."
SILENCE_DBFS = -55.0


class ReelVoiceoverError(RuntimeError):
    """A Reel cannot be rendered or published without usable narration audio."""


@dataclass
class ReelResult:
    path: Path
    voiced: bool
    footage: str      # "pexels" | "pixabay" | "local" | "commons" (photos only) | "animated"
    seconds: float
    images_credit: str | None = None   # attribution for real photos (required by CC BY): put it in the caption
    style: dict | None = None          # the look used for this Reel (kept so the next Reel looks different)

    @property
    def credit(self) -> str | None:
        """Courtesy credit for the caption (only for stock-site footage)."""
        return {"pexels": "Pexels", "pixabay": "Pixabay"}.get(self.footage)


def narration_text(content: dict) -> str:
    sentences = [s.strip() for s in content["narration"] if s.strip()]
    if len(" ".join(sentences).split()) < 80:
        sentences.append(CTA_SENTENCE)
    return " ".join(sentences)


def default_photo_sources() -> list:
    out = []
    if config.PHOTO_DIR:                       # your own licensed/permitted photos always come first
        out.append(LocalPhotos())
    if "commons" in config.PHOTO_SOURCES:
        out.append(CommonsClient())
    if "openverse" in config.PHOTO_SOURCES:
        out.append(OpenverseClient())
    return out


def _voice_options(tts, style: ReelStyle) -> dict:
    """Voice/rate/pitch for this Reel, passed only to voice functions that accept them (test doubles may not)."""
    if not (style.voice or style.rate):
        return {}
    try:
        params = inspect.signature(tts).parameters
    except (TypeError, ValueError):
        return {}
    ok = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    opts = {"voice": style.voice, "rate": style.rate, "pitch": style.pitch}
    return {k: v for k, v in opts.items() if ok or k in params}


def validate_reel_result(result: ReelResult) -> None:
    """Reject silent or invalid Reel output, including results from injected Reel makers."""
    if not getattr(result, "voiced", False):
        raise ReelVoiceoverError("Reel maker returned no voiceover")
    path = Path(getattr(result, "path", ""))
    if not path.is_file() or path.stat().st_size == 0:
        raise ReelVoiceoverError("Reel audio output is missing or empty")
    try:
        info = probe(path)
    except Exception as e:  # noqa: BLE001
        raise ReelVoiceoverError(f"Reel audio output is invalid: {type(e).__name__}: {e}") from e
    duration = info.get("duration")
    if not info.get("video") or not info.get("audio") or not isinstance(duration, (int, float)) \
            or not math.isfinite(duration) or duration <= 1.0:
        raise ReelVoiceoverError("Reel output must contain valid video and audio with a duration over 1 second")
    _validate_audio_content(path)


def _validate_audio_content(path: Path) -> None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise ReelVoiceoverError("ffmpeg is required to validate Reel audio")
    try:
        result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path), "-vn", "-af", "volumedetect",
                                 "-f", "null", "-"], capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        raise ReelVoiceoverError(f"Reel audio could not be measured: {type(e).__name__}: {e}") from e
    match = re.search(r"mean_volume:\s*(-?inf|-?[\d.]+) dB", result.stderr)
    if result.returncode or not match or match[1] == "-inf" or float(match[1]) <= SILENCE_DBFS:
        raise ReelVoiceoverError("Reel audio is silent or below the narration level threshold")


def validate_voiceover(voice: Voiceover | None) -> float:
    if voice is None:
        raise ReelVoiceoverError("voiceover generation returned no audio")
    path = Path(getattr(voice, "path", ""))
    if not path.is_file() or path.stat().st_size == 0:
        raise ReelVoiceoverError("voiceover audio is missing or empty")
    try:
        info = probe(path)
    except Exception as e:  # noqa: BLE001
        raise ReelVoiceoverError(f"voiceover audio is invalid: {type(e).__name__}: {e}") from e
    duration = info.get("duration")
    speech = getattr(voice, "duration", None)
    if (not info.get("audio") or not isinstance(duration, (int, float)) or not math.isfinite(duration)
            or duration <= 1.0 or not isinstance(speech, (int, float)) or not math.isfinite(speech)
            or speech <= 1.0 or not getattr(voice, "words", None)):
        raise ReelVoiceoverError("voiceover must contain valid audio, word timings, and a duration over 1 second")
    tolerance = max(0.5, float(speech) * 0.1)
    if abs(float(duration) - float(speech)) > tolerance:
        raise ReelVoiceoverError("voiceover duration does not match the probed audio duration")
    _validate_audio_content(path)
    return float(speech)


def mux_voiceover(video_path: Path, voice: Voiceover, out_path: Path, *, background_volume: float = 1.0) -> Path:
    """Keep a legacy card slideshow intact while replacing its silent/music-only track with narration + music."""
    speech = validate_voiceover(voice)
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise VideoError("ffmpeg not found - run: pip install imageio-ffmpeg")
    source_info = probe(Path(video_path))
    source_duration = source_info.get("duration")
    if not source_info.get("video") or not source_info.get("audio") or not isinstance(source_duration, (int, float)):
        raise ReelVoiceoverError("video to narrate must contain valid video and an audio stream")
    target_duration = max(float(source_duration), LEAD + speech + OUTRO)
    if target_duration > config.VIDEO_MAX_SECONDS:
        raise ReelVoiceoverError(f"narrated video would exceed the {config.VIDEO_MAX_SECONDS:g} second limit")
    pad_duration = max(0.0, target_duration - float(source_duration))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lead_ms = int(LEAD * 1000)
    background_volume = max(0.0, min(float(background_volume), 1.0))
    filt = (f"[0:v]tpad=stop_mode=clone:stop_duration={pad_duration:.3f}[v];"
            f"[0:a]apad=whole_dur={target_duration:.3f},volume={background_volume}[bg];"
            f"[1:a]adelay={lead_ms}:all=1,apad[voice];"
            "[bg][voice]amix=inputs=2:duration=first:dropout_transition=2[a]")
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(video_path), "-i", str(voice.path),
           "-filter_complex", filt, "-map", "[v]", "-map", "[a]", "-t", f"{target_duration:.3f}",
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", str(out_path)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        raise ReelVoiceoverError(f"could not add voiceover to card Reel: {type(e).__name__}: {e}") from e
    validate_reel_result(ReelResult(out_path, True, "animated", target_duration))
    return out_path


def make_reel(content: dict, cluster: dict, out_path: Path, *, providers: list | None = None, commons=None,
              photo_sources: list | None = None, tts=synthesize, music_path: str | None = None,
              authentic_photos: list | None = None, stock_ok: bool = True, voiceover: bool = True) -> ReelResult:
    """authentic_photos: already vetted/licensed Photo objects from the media layer (skips the old blind photo search).
    stock_ok=False: no stock footage at all (an event story must not be dressed up with generic clips)."""
    if not find_ffmpeg():
        raise VideoError("ffmpeg not found - run: pip install imageio-ffmpeg")
    tmp = Path(tempfile.mkdtemp(prefix="reel_"))
    try:
        text = narration_text(content)
        style = ReelStyle.from_dict(cluster.get("reel_style")) if cluster.get("reel_style") else ReelStyle()
        if not voiceover:
            raise ReelVoiceoverError("Reel voiceover is disabled")
        try:
            voice = tts(text, tmp, **_voice_options(tts, style))
        except Exception as e:  # noqa: BLE001 - surface provider failure as an explicit Reel failure
            raise ReelVoiceoverError(f"voiceover generation failed: {type(e).__name__}: {e}") from e
        speech = validate_voiceover(voice)
        if LEAD + speech + OUTRO > 85.0:
            raise ReelVoiceoverError("voiceover narration exceeds the Reel duration limit")
        words = getattr(voice, "words", None)
        words = [(w, s + LEAD, e + LEAD) for w, s, e in words]
        total = min(max(LEAD + speech + OUTRO, 8.0), 85.0)

        n_seg, _ = segment_plan(total)
        story_text = " ".join([cluster["lead"].get("title", ""), cluster["lead"].get("summary", "")[:300],
                               content.get("hook", ""), " ".join(content["narration"])])
        queries = queries_for(cluster["category"], content.get("broll_queries"), story_text)
        log.info("Footage search terms: %s", " | ".join(queries[:6]))
        seed = str(cluster.get("fingerprint", ""))   # same story -> same footage, so preview == published

        photos = []
        if authentic_photos is not None:
            photos = list(authentic_photos)[:4]
        elif config.REAL_PHOTOS:
            sources = photo_sources if photo_sources is not None else ([commons] if commons is not None
                                                                         else default_photo_sources())
            pq = photo_queries_for(story_text, content.get("photo_queries"))
            ctx = context_terms(story_text + " " + " ".join(content["narration"]), " ".join(pq), cluster["category"])
            for src in sources:
                if len(photos) >= 4 or not getattr(src, "enabled", True):
                    continue
                try:
                    photos += src.fetch_photos(pq, tmp / "photos" / getattr(src, "name", "src"),
                                               wanted=4 - len(photos), rng=random.Random(seed), context=ctx)
                except Exception as e:  # noqa: BLE001 - a photo-source problem must never fail the Reel
                    log.warning("Photo source %s failed: %s", getattr(src, "name", "?"), str(e)[:120])
            photos = photos[:4]   # hard cap, whatever a source returns

        footage, clips = "animated", []
        want_clips = 2 if len(photos) >= 2 else min(n_seg, 5)
        for provider in ((providers if providers is not None else configured_providers()) if stock_ok else []):
            if not getattr(provider, "enabled", True):
                continue
            try:
                clips = provider.fetch_clips(queries, tmp / "stock", wanted=want_clips,
                                             rng=random.Random(seed + getattr(provider, "name", "")))
            except Exception as e:  # noqa: BLE001 - a stock outage must never fail the Reel
                log.warning("Footage provider %s failed: %s", getattr(provider, "name", "?"), str(e)[:120])
                clips = []
            if clips:
                footage = getattr(provider, "name", "stock")
                for ch in getattr(provider, "chosen", []):
                    log.info("Footage for '%s': %s", ch["query"], ch["tags"])
                break
        if stock_ok and not clips and not photos and config.STOCK_PROVIDER != "none":
            clips = local_clips(config.BROLL_DIR, wanted=min(n_seg, 5), category=cluster["category"], queries=queries,
                                rng=random.Random(seed))
            footage = "local" if clips else footage
        bg = None
        try:
            if photos:  # documentary mode: real photos first, relevant footage interleaved
                items, pi, ci = [], 0, 0
                while pi < len(photos) or ci < len(clips):
                    if pi < len(photos):
                        items.append({"kind": "photo", "path": photos[pi].path,
                                      "credit": photos[pi].credit if photos[pi].needs_credit else ""})
                        pi += 1
                    if ci < len(clips):
                        items.append({"kind": "clip", "path": clips[ci], "credit": ""})
                        ci += 1
                bg = prepare_mixed_background(items, total, tmp, **({"flip": style.pan_flip} if style.pan_flip else {}))
                footage = footage if clips else "commons"
            elif clips:
                bg = prepare_footage_background(clips, total, tmp)
        except VideoError as e:
            log.warning("Footage could not be processed (%s) -> animated background", str(e)[:160])
            footage, photos = "animated", []
        if bg is None:
            bg = prepare_animated_background(cluster["category"], total, tmp)

        music = Path(music_path) if music_path and Path(music_path).exists() else None
        if music_path and not music:
            log.warning("MUSIC_PATH '%s' not found -> no music", music_path)
        compose_reel(bg, content, cluster, words, total, voice, music, Path(out_path),
                     **({"style": style} if cluster.get("reel_style") else {}))
        result = ReelResult(Path(out_path), True, footage, total, images_credit(photos),
                    style.to_dict() if cluster.get("reel_style") else None)
        validate_reel_result(result)
        log.info("Reel ready: %.1fs, voice=%s, footage=%s, photos=%d", total, bool(voice), footage, len(photos))
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
