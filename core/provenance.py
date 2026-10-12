"""Media provenance: what a picture/video is, where it came from, whether we may reuse it, and why we trust it.

Every asset the editorial engine considers becomes a `MediaAsset`. Nothing is published unless its `permission`
is one of ALLOWED_PERMISSIONS and its `confidence` passes the threshold. The same record is written into the
editorial decision record, so every post can be audited later.

Permission values
  public_domain        CC0 / Public Domain Mark / US-government style public domain
  open_licence         CC BY (attribution shown on the post and in the caption), GODL-India
  official_permitted   from a domain listed in OFFICIAL_MEDIA_DOMAINS (publisher/agency that allows reuse) or a
                       rights file you supplied for your own authorised video
  owned                a file in your own library (PHOTO_DIR / VIDEO_DIR) that you declared you may use
  unverified           no documented right to reuse -> NEVER downloaded or published
  rejected             a licence that forbids our use (NC, ND, share-alike, fair use, GFDL, trademark)

Role values (what the media shows)
  event       depicts this event (right people/place AND the right time)
  subject     depicts the right person/place/product but not this event: published only as a labelled "File photo"
  illustrative generic footage/photo that merely looks related: never presented as the event
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

from .commons import license_ok, needs_attribution

ALLOWED_PERMISSIONS = {"public_domain", "open_licence", "official_permitted", "owned"}
ROLES = ("event", "subject", "illustrative")


class MediaRejected(Exception):
    """A media candidate that must not be used; `reason` is logged and stored in the decision record."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class MediaAsset:
    kind: str                      # "photo" | "video"
    source: str                    # provider: official | local | commons | openverse | stock ...
    title: str
    original_url: str              # where the file itself was fetched from
    page_url: str = ""             # human-readable page (attribution link)
    creator: str = ""
    license: str = ""              # as reported, e.g. "CC BY 4.0"
    permission: str = "unverified"
    attribution: str = ""          # text to show/print, e.g. "Jane Doe (CC BY 4.0)"
    attribution_required: bool = False
    caption_credit: str = ""       # caption wording incl. a source URL where the licence requires it
    role: str = "illustrative"
    confidence: float = 0.0
    rationale: str = ""
    story_id: str = ""
    retrieved_at: str = ""
    media_date: str = ""           # date/year the media was made, when the source says so
    width: int = 0
    height: int = 0
    duration: float = 0.0
    path: str = ""                 # local file once downloaded/validated
    sha256: str = ""
    phash: int = 0
    has_audio: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("path", None)        # local temp paths mean nothing later
        return d

    @property
    def credit_line(self) -> str:
        return self.caption_credit or self.attribution

    def usable(self) -> bool:
        return self.permission in ALLOWED_PERMISSIONS


@dataclass
class Rejection:
    title: str
    source: str
    reason: str
    kind: str = "photo"
    url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ licence classification
def permission_for_licence(short: str) -> tuple[str, bool]:
    """(permission, attribution_required) for a licence label. Anything unclear is `unverified`, never allowed."""
    s = (short or "").strip()
    if not s:
        return "unverified", False
    low = s.lower().replace("-", " ").replace("_", " ")
    if license_ok(s):
        if low.startswith(("cc0", "public domain", "pdm")) or low == "pd" or low.startswith("pd "):
            return "public_domain", False
        return "open_licence", needs_attribution(s)
    if (set(low.split()) & {"sa", "nc", "nd", "gfdl"} or "share" in low or "non free" in low or "fair use" in low
            or "all rights reserved" in low):
        return "rejected", False
    return "unverified", False


def licence_from_url(url: str) -> str:
    """A feed that declares a Creative Commons licence URL (e.g. https://creativecommons.org/licenses/by/4.0/)."""
    m = re.search(r"creativecommons\.org/(licenses|publicdomain)/([a-z\-]+)/?([\d.]*)", url or "", re.I)
    if not m:
        return ""
    kind, name, ver = m.group(1).lower(), m.group(2).lower(), m.group(3)
    if kind == "publicdomain":
        return "CC0" if name == "zero" else "Public Domain"
    return f"CC {name.upper()} {ver}".strip()


def domain_of(url: str) -> str:
    return urlsplit(url or "").netloc.lower().removeprefix("www.")


def official_domain(url: str, allowed: list[str]) -> bool:
    d = domain_of(url)
    return bool(d) and any(d == a or d.endswith("." + a) for a in allowed)


# ------------------------------------------------------------------ file validation
def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dhash(img: Image.Image, size: int = 8) -> int:
    """64-bit difference hash: near-identical pictures (resized/re-encoded copies) differ by only a few bits."""
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.get_flattened_data()) if hasattr(g, "get_flattened_data") else list(g.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            bits = (bits << 1) | (1 if px[row * (size + 1) + col] > px[row * (size + 1) + col + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def is_duplicate_image(h: int, seen: list[int], max_distance: int = 6) -> bool:
    return any(hamming(h, s) <= max_distance for s in seen)


def validate_image_file(path: Path, *, min_width: int, min_height: int, max_bytes: int = 25 * 1024 * 1024) -> tuple[int, int, int]:
    """Open the downloaded file for real: truncated, HTML-error-page, tiny or oversized files are rejected.
    Returns (width, height, perceptual hash)."""
    p = Path(path)
    if not p.is_file() or p.stat().st_size < 5_000:
        raise MediaRejected("download is empty or too small to be a photograph")
    if p.stat().st_size > max_bytes:
        raise MediaRejected("file larger than the size limit")
    try:
        with Image.open(p) as im:
            im.verify()
        with Image.open(p) as im:
            im.load()
            w, h = im.size
            ph = dhash(im)
    except Exception as e:  # noqa: BLE001 - any decode problem means "not a usable image"
        raise MediaRejected(f"not a valid image ({type(e).__name__})") from e
    if w < min_width or h < min_height:
        raise MediaRejected(f"resolution {w}x{h} below the minimum {min_width}x{min_height}")
    return w, h, ph


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class MediaPlan:
    """Everything media retrieval found for one story."""
    story_id: str
    photos: list[MediaAsset] = field(default_factory=list)          # accepted, best first (event before subject)
    videos: list[MediaAsset] = field(default_factory=list)          # accepted authorised videos
    illustrative_clips: int = 0                                     # generic stock clips available (never "event" media)
    rejected: list[Rejection] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    providers: dict = field(default_factory=dict)                   # provider -> "ok" | "skipped: why" | "error: why"

    @property
    def event_photos(self) -> list[MediaAsset]:
        return [a for a in self.photos if a.role == "event"]

    @property
    def subject_photos(self) -> list[MediaAsset]:
        return [a for a in self.photos if a.role == "subject"]

    @property
    def event_videos(self) -> list[MediaAsset]:
        return [a for a in self.videos if a.role == "event"]

    def summary(self) -> dict:
        return {"photos": [a.to_dict() for a in self.photos], "videos": [a.to_dict() for a in self.videos],
                "rejected": [r.to_dict() for r in self.rejected][:25], "queries": self.queries,
                "providers": self.providers, "illustrative_clips_available": self.illustrative_clips}
