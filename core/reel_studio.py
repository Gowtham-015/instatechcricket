"""Orchestrates one dynamic Reel: narration -> voiceover -> footage -> render. Every network-dependent step
(voice, stock footage) degrades gracefully, so a Reel is always produced as long as ffmpeg exists."""
from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config
from .reel_renderer import (LEAD, OUTRO, compose_reel, prepare_animated_background, prepare_footage_background,
                            segment_plan)
from .stock import configured_providers, local_clips, queries_for
from .tts import Voiceover, distribute_words, reading_speed_duration, synthesize
from .video_renderer import VideoError, find_ffmpeg

log = logging.getLogger("reel_studio")
CTA_SENTENCE = "Follow for more updates."


@dataclass
class ReelResult:
    path: Path
    voiced: bool
    footage: str      # "pexels" | "pixabay" | "local" | "animated"
    seconds: float

    @property
    def credit(self) -> str | None:
        """Courtesy credit for the caption (only for stock-site footage)."""
        return {"pexels": "Pexels", "pixabay": "Pixabay"}.get(self.footage)


def narration_text(content: dict) -> str:
    sentences = [s.strip() for s in content["narration"] if s.strip()]
    if len(" ".join(sentences).split()) < 80:
        sentences.append(CTA_SENTENCE)
    return " ".join(sentences)


def make_reel(content: dict, cluster: dict, out_path: Path, *, providers: list | None = None,
              tts=synthesize, music_path: str | None = None) -> ReelResult:
    if not find_ffmpeg():
        raise VideoError("ffmpeg not found - run: pip install imageio-ffmpeg")
    tmp = Path(tempfile.mkdtemp(prefix="reel_"))
    try:
        text = narration_text(content)
        voice: Voiceover | None = tts(text, tmp)
        if voice and voice.words:
            speech, words = voice.duration, voice.words
        else:
            voice, speech = None, reading_speed_duration(text)
            words = distribute_words(text, speech)
        words = [(w, s + LEAD, e + LEAD) for w, s, e in words]
        total = min(max(LEAD + speech + OUTRO, 8.0), 85.0)

        n_seg, _ = segment_plan(total)
        story_text = " ".join([cluster["lead"].get("title", ""), cluster["lead"].get("summary", "")[:300],
                               content.get("hook", ""), " ".join(content["narration"])])
        queries = queries_for(cluster["category"], content.get("broll_queries"), story_text)
        log.info("Footage search terms: %s", " | ".join(queries[:6]))
        footage, clips = "animated", []
        for provider in (providers if providers is not None else configured_providers()):
            if not getattr(provider, "enabled", True):
                continue
            try:
                clips = provider.fetch_clips(queries, tmp / "stock", wanted=min(n_seg, 5))
            except Exception as e:  # noqa: BLE001 - a stock outage must never fail the Reel
                log.warning("Footage provider %s failed: %s", getattr(provider, "name", "?"), str(e)[:120])
                clips = []
            if clips:
                footage = getattr(provider, "name", "stock")
                for ch in getattr(provider, "chosen", []):
                    log.info("Footage for '%s': %s", ch["query"], ch["tags"])
                break
        if not clips and config.STOCK_PROVIDER != "none":
            clips = local_clips(config.BROLL_DIR, wanted=min(n_seg, 5), category=cluster["category"], queries=queries)
            footage = "local" if clips else footage
        bg = None
        if clips:
            try:
                bg = prepare_footage_background(clips, total, tmp)
            except VideoError as e:
                log.warning("Footage could not be processed (%s) -> animated background", str(e)[:160])
                footage = "animated"
        if bg is None:
            bg = prepare_animated_background(cluster["category"], total, tmp)

        music = Path(music_path) if music_path and Path(music_path).exists() else None
        if music_path and not music:
            log.warning("MUSIC_PATH '%s' not found -> no music", music_path)
        compose_reel(bg, content, cluster, words, total, voice, music, Path(out_path))
        log.info("Reel ready: %.1fs, voice=%s, footage=%s", total, bool(voice), footage)
        return ReelResult(Path(out_path), bool(voice), footage, total)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
