"""Voiceover: text -> audio file + per-word timings (used to sync the animated captions).

Providers (TTS_PROVIDER=auto tries them in this order, anything failing falls through to the next):
  edge   : Microsoft Edge neural voices via the `edge-tts` package. Free, no key, gives word timings.
           (Unofficial endpoint of the Edge browser's read-aloud feature - it can change without notice.)
  gemini : Gemini TTS with your existing GEMINI_API_KEY (official, subject to your quota).
  none   : no voice; the video is then captioned at reading speed.
A failed voice NEVER fails the post: synthesize() returns None and the Reel is built without narration.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

from . import config
from .health import mask_secrets
from .video_renderer import find_ffmpeg, probe

log = logging.getLogger("tts")
TICK = 1e7  # edge-tts offsets/durations are in 100-nanosecond ticks


@dataclass
class Voiceover:
    path: Path
    duration: float
    words: list[tuple[str, float, float]]  # (word, start_s, end_s)
    provider: str = ""


def split_words(text: str) -> list[str]:
    return [w for w in re.split(r"\s+", text.strip()) if w]


def _weight(word: str) -> float:
    w = len(re.sub(r"\W", "", word)) + 1.5
    if word.endswith((".", "!", "?")):
        w += 4.0   # sentence pause
    elif word.endswith((",", ";", ":")):
        w += 2.0   # clause pause
    return w


def distribute_words(text: str, duration: float, start: float = 0.0) -> list[tuple[str, float, float]]:
    """Fallback timing: spread words over `duration` proportionally to length (+ pauses at punctuation)."""
    words = split_words(text)
    if not words or duration <= 0:
        return []
    weights = [_weight(w) for w in words]
    scale = duration / sum(weights)
    out, t = [], start
    for w, wt in zip(words, weights):
        end = t + wt * scale
        out.append((w, t, end))
        t = end
    return out


def words_from_boundaries(text: str, bounds: list[tuple[float, float, str]]) -> list[tuple[str, float, float]]:
    """Map the script's own words onto edge-tts boundary events (robust to tokenisation differences)."""
    words = split_words(text)
    if len(bounds) == len(words):
        return [(w, b[0], b[0] + b[1]) for w, b in zip(words, bounds)]
    return []  # mismatch -> caller falls back to proportional timing


def _silences(path: Path, duration: float) -> list[tuple[float, float]]:
    ff = find_ffmpeg()
    if not ff:
        return []
    r = subprocess.run([ff, "-hide_banner", "-i", str(path), "-af", "silencedetect=noise=-35dB:d=0.15",
                        "-f", "null", "-"], capture_output=True, text=True, timeout=90)
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    out = []
    for i, st in enumerate(starts):
        en = ends[i] if i < len(ends) else duration
        out.append((max(0.0, st), min(en, duration)))
    return out


def align_words(text: str, path: Path, duration: float) -> list[tuple[str, float, float]]:
    """Word timings for voices WITHOUT word boundaries (Gemini TTS): use the real pauses in the audio.
    1) trim leading/trailing silence  2) the N-1 longest internal pauses are the sentence breaks
    3) spread each sentence's words proportionally inside its own spoken span. Any problem -> plain proportional."""
    try:
        sentences = [x for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x]
        sil = _silences(path, duration)
        start = sil[0][1] if sil and sil[0][0] <= 0.05 else 0.0
        end = sil[-1][0] if sil and sil[-1][1] >= duration - 0.05 else duration
        if end - start < 1.0:
            raise ValueError("no usable speech span")
        internal = [(a, b) for a, b in sil if a > start + 0.05 and b < end - 0.05]
        n = len(sentences)
        if n > 1 and len(internal) >= n - 1:
            gaps = sorted(sorted(internal, key=lambda g: g[1] - g[0], reverse=True)[: n - 1])
            starts = [start] + [g[1] for g in gaps]
            ends = [g[0] for g in gaps] + [end]
            words: list[tuple[str, float, float]] = []
            for sent, a, b in zip(sentences, starts, ends):
                words += distribute_words(sent, max(b - a, 0.3), a)
            return words
        return distribute_words(text, end - start, start)
    except Exception as e:  # noqa: BLE001
        log.info("Audio alignment unavailable (%s) -> proportional timing", type(e).__name__)
        return distribute_words(text, duration)


def reading_speed_duration(text: str, wps: float = 2.7) -> float:
    return max(len(split_words(text)) / wps, 2.0)


# ---------------------------------------------------------------- providers
async def _edge_collect(text: str, voice: str, rate: str, pitch: str = "+0Hz"):
    import edge_tts
    audio, bounds = bytearray(), []
    try:
        com = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, boundary="WordBoundary")
    except TypeError:   # very old edge-tts without pitch support
        com = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    async for chunk in com.stream():
        kind = chunk.get("type")
        if kind == "audio":
            audio.extend(chunk["data"])
        elif kind == "WordBoundary":
            bounds.append((chunk["offset"] / TICK, chunk["duration"] / TICK, chunk.get("text", "")))
    return bytes(audio), bounds


def _edge(text: str, out_dir: Path, voice: str = "", rate: str = "", pitch: str = "") -> Voiceover | None:
    audio, bounds = asyncio.run(asyncio.wait_for(
        _edge_collect(text, voice or config.TTS_VOICE, rate or config.TTS_RATE, pitch or "+0Hz"), 90))
    if not audio:
        return None
    path = out_dir / "voice.mp3"
    path.write_bytes(audio)
    info = {}
    try:
        info = probe(path)
    except Exception:  # noqa: BLE001
        pass
    last_end = max((b[0] + b[1] for b in bounds), default=0.0)
    duration = info.get("duration") or (last_end + 0.2)
    words = words_from_boundaries(text, bounds) or align_words(text, path, float(duration))
    return Voiceover(path, float(duration), words, "edge")


def _gemini(text: str, out_dir: Path, **_ignored) -> Voiceover | None:
    if not config.GEMINI_API_KEY:
        return None
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    names = [m.name for m in client.models.list() if "tts" in m.name.lower()]
    if not names:
        return None
    model = sorted(names)[-1].split("/")[-1]
    resp = client.models.generate_content(
        model=model,
        contents="Read this like an energetic news reporter, clearly and at a brisk pace: " + text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=config.GEMINI_TTS_VOICE))),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
    pcm = resp.candidates[0].content.parts[0].inline_data.data
    path = out_dir / "voice.wav"
    with wave.open(str(path), "wb") as w:  # Gemini TTS returns raw 16-bit mono PCM at 24 kHz
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    duration = len(pcm) / (2 * 24000)
    return Voiceover(path, duration, align_words(text, path, duration), "gemini")


def synthesize(text: str, out_dir: Path, provider: str | None = None, voice: str = "", rate: str = "",
               pitch: str = "") -> Voiceover | None:
    provider = (provider or config.TTS_PROVIDER).lower()
    if provider == "none" or not text.strip():
        return None
    chain = {"auto": [("edge", _edge), ("gemini", _gemini)], "edge": [("edge", _edge)],
             "gemini": [("gemini", _gemini)]}.get(provider, [])
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, fn in chain:
        try:
            vo = fn(text, out_dir, voice=voice, rate=rate, pitch=pitch)
            if vo and vo.duration > 1.0:
                log.info("Voiceover by %s: %.1fs, %d words", name, vo.duration, len(vo.words))
                return vo
        except Exception as e:  # noqa: BLE001 - voice problems must never fail a post
            log.warning("TTS provider %s failed: %s: %s", name, type(e).__name__, mask_secrets(str(e))[:160])
    log.warning("No voiceover available -> captions at reading speed")
    return None
