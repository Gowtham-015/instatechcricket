"""Per-Reel variety: a different voice, caption colour, headline layout, colour grade, outro and script opening
for each Reel, so followers never feel they are watching the same Reel again.

How it works
* The look is picked from small pools with a random generator seeded by the story fingerprint, so a preview and
  the published Reel of the SAME story look identical.
* The last few published looks are remembered (data/style_history.json). Every pool skips what the previous Reels
  used, so two Reels in a row never share the voice, caption colour or headline layout.
* Every option stays inside Instagram's safe zones (the renderer clamps positions), and nothing here touches the facts.
"""
from __future__ import annotations

import logging
import random
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import config
from .storage import read_json, write_json

log = logging.getLogger("variety")

HISTORY_FILE = "style_history.json"
KEEP = 8

HIGHLIGHTS = {            # colour of the word being spoken ("accent" = the category colour)
    "accent": None, "yellow": (255, 214, 10), "cyan": (0, 229, 255), "lime": (163, 230, 53),
    "pink": (255, 89, 160), "orange": (255, 138, 0),
}
GRADES = {                # gentle ffmpeg colour grading applied to the footage only (never the text)
    "natural": "", "vivid": "eq=saturation=1.22:contrast=1.07", "cool": "eq=saturation=0.92:contrast=1.05:gamma=0.97",
    "warm": "colorbalance=rs=0.06:gs=0.01:bs=-0.06,eq=saturation=1.08", "punchy": "eq=contrast=1.12:saturation=1.1",
    "vignette": "vignette=PI/5",
}
CAPTION_Y = {"low": 1190, "mid": 1150, "high": 1110}      # all clear of the bottom UI and of the headline
HOOK_Y = {"top": 330, "mid": 380, "low": 420}
HOOK_LAYOUTS = ("left", "center", "box")
HOOK_ANIMS = ("fade", "rise")
OUTROS = ("Follow for daily updates", "Want more like this?", "Share this with a friend", "Save this and follow")
OPENERS = (
    "Open with the single most surprising fact from the source.",
    "Open with a direct question to the viewer that the source answers.",
    "Open with the most important number or name from the source, then explain it.",
    "Open with what this means for ordinary people, then give the news.",
    "Open with a short, punchy statement of what has just happened.",
)


@dataclass(frozen=True)
class ReelStyle:
    name: str = "classic"
    voice: str = ""            # "" = use TTS_VOICE
    rate: str = ""             # "" = use TTS_RATE
    pitch: str = "+0Hz"
    highlight: str = "accent"
    caption_pos: str = "mid"
    caption_size: int = 112
    caption_words: int = 3
    caption_upper: bool = False
    hook_layout: str = "left"
    hook_pos: str = "mid"
    hook_anim: str = "fade"
    grade: str = "natural"
    outro: int = 0
    pan_flip: bool = False
    opener: int = 0

    # ------------------------------------------------------------ resolved values (always safe)
    @property
    def highlight_rgb(self):
        return HIGHLIGHTS.get(self.highlight)

    @property
    def caption_y(self) -> int:
        return CAPTION_Y.get(self.caption_pos, 1150)

    @property
    def hook_y(self) -> int:
        return HOOK_Y.get(self.hook_pos, 380)

    @property
    def grade_filter(self) -> str:
        return GRADES.get(self.grade, "")

    @property
    def outro_text(self) -> str:
        return OUTROS[self.outro % len(OUTROS)]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "ReelStyle":
        if not isinstance(d, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        try:
            return cls(**{k: v for k, v in d.items() if k in known})
        except TypeError:
            return cls()


CLASSIC = ReelStyle()      # the original look: used when variety is switched off


# ------------------------------------------------------------------ pools
def voice_pool() -> list[str]:
    return list(config.TTS_VOICES) or [config.TTS_VOICE]


def _rate_pool() -> list[str]:
    try:
        base = int(config.TTS_RATE.replace("%", "").replace("+", "").strip() or 0)
    except ValueError:
        base = 6
    return [f"{max(-10, min(25, base + d)):+d}%" for d in (-4, 0, 4, 7)]


def _choose(rng: random.Random, options, recent_values, keep_last: int):
    """Random option that differs from the last `keep_last` Reels (falls back to any option if all were used)."""
    avoid = set(recent_values[:keep_last])
    pool = [o for o in options if o not in avoid] or list(options)
    return rng.choice(pool)


def pick_style(seed: str, recent: list[dict] | None = None) -> ReelStyle:
    rng = random.Random(f"{seed}:style")
    recent = recent or []

    def past(key: str) -> list:
        return [r.get(key) for r in recent]

    voice = _choose(rng, voice_pool(), past("voice"), 1)
    highlight = _choose(rng, list(HIGHLIGHTS), past("highlight"), 2)
    hook_layout = _choose(rng, HOOK_LAYOUTS, past("hook_layout"), 1)
    caption_pos = _choose(rng, list(CAPTION_Y), past("caption_pos"), 1)
    grade = _choose(rng, list(GRADES), past("grade"), 2)
    outro = _choose(rng, range(len(OUTROS)), past("outro"), 2)
    opener = _choose(rng, range(len(OPENERS)), past("opener"), 2)
    style = ReelStyle(
        voice=voice, rate=_choose(rng, _rate_pool(), past("rate"), 1),
        pitch=_choose(rng, ("-3Hz", "+0Hz", "+3Hz"), past("pitch"), 1),
        highlight=highlight, caption_pos=caption_pos,
        caption_size=_choose(rng, (104, 112, 120), past("caption_size"), 1),
        caption_words=_choose(rng, (2, 3), past("caption_words"), 1),
        caption_upper=rng.random() < 0.4,
        hook_layout=hook_layout, hook_pos=_choose(rng, list(HOOK_Y), past("hook_pos"), 1),
        hook_anim=_choose(rng, HOOK_ANIMS, past("hook_anim"), 1),
        grade=grade, outro=outro, pan_flip=rng.random() < 0.5, opener=opener)
    short = voice.split("-")[-1].replace("Neural", "") if voice else "default"
    return ReelStyle(**{**style.to_dict(), "name": f"{short}/{highlight}/{hook_layout}/{grade}"})


def opener_hint(style: ReelStyle) -> str:
    return OPENERS[style.opener % len(OPENERS)]


# ------------------------------------------------------------------ memory of recent looks
def load_recent(data_dir: Path) -> list[dict]:
    data = read_json(Path(data_dir) / HISTORY_FILE, [])
    return [d for d in data if isinstance(d, dict)][:KEEP]


def remember(data_dir: Path, style: ReelStyle) -> None:
    try:
        recent = load_recent(data_dir)
        write_json(Path(data_dir) / HISTORY_FILE, [style.to_dict()] + recent[: KEEP - 1])
    except Exception as e:  # noqa: BLE001 - remembering a look must never fail a post
        log.warning("Could not save style history: %s", str(e)[:120])
