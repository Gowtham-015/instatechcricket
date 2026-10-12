"""Story-to-media retrieval: find pictures/videos that depict THIS story, prove we may reuse them, reject the rest.

Pipeline for one story
  1. queries   built from the story's own entities (people, teams, institutions, products, places) - never from the
               bare category ("politics", "cricket", "technology")
  2. providers tried in MEDIA_SOURCE_ORDER:  official -> local -> commons -> openverse  (+ authorised videos)
  3. every candidate goes through `assess()` BEFORE anything is downloaded:
       - documented permission (licence / official domain / rights file)      -> else rejected, never fetched
       - named entity must appear in the media's own title/description/categories
       - wrong person (title is about someone else), wrong match ("A vs B" is not this match), wrong place,
         wrong tone, wrong year -> rejected or demoted from "event" to "subject" (shown as a File photo)
       - confidence >= MEDIA_MIN_CONFIDENCE
  4. accepted candidates are downloaded and validated for real (decodable, resolution, duplicate pictures)
  5. everything - accepted AND rejected, with reasons - is kept in the MediaPlan for the decision record

Generic stock footage is never "event" media: it is counted only as `illustrative_clips`.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from . import config
from .commons import (CommonsClient, GEO_WORDS, INSTITUTION_WORDS, PhotoSource, _meta, context_terms, entity_queries,
                      geo_conflict, license_ok, needs_attribution, photo_queries_for, tone_conflict)
from .health import mask_secrets
from .localphotos import LocalPhotos
from .openverse import OpenverseClient
from .provenance import (ALLOWED_PERMISSIONS, MediaAsset, MediaPlan, MediaRejected, Rejection, domain_of, file_sha256,
                         is_duplicate_image, licence_from_url, now_iso, official_domain, permission_for_licence,
                         validate_image_file)
from .dedupe import STOP
from .stock import ABSTRACT_WORDS, GENERIC_WORDS, kw, overlap, query_terms

log = logging.getLogger("media")
MAX_VIDEO_BYTES = 150 * 1024 * 1024
_YEAR = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
# "India vs Australia": at most three capitalised words on each side, stopping at digits, lowercase words and punctuation,
# so trailing words of the title ("... highlights India fans") are never mistaken for part of the fixture.
_VS = re.compile(r"((?:[A-Z][\w.&'’-]*\s+){0,2}[A-Z][\w.&'’-]*)\s+(?:vs\.?|v\.?|versus)\s+"
                 r"([A-Z][\w.&'’-]*(?:\s+[A-Z][\w.&'’-]*){0,2})")
_UA = {"User-Agent": f"InstaNewsAgent/1.0 (news; {config.ACCOUNT_HANDLE}) python-requests"}
_TITLE_NOISE = {"final", "finals", "match", "highlights", "women", "womens", "men", "mens", "team", "cricket", "football",
                "series", "tour", "cup", "league", "round", "semi", "quarter", "game", "games", "day", "innings", "test",
                "odi", "t20", "ipl", "world", "asian", "asia", "gold", "medal"}


# ----------------------------------------------------------------------------- assessment
@dataclass
class Assessment:
    accepted: bool
    confidence: float = 0.0
    role: str = "illustrative"
    rationale: str = ""
    reason: str = ""             # why it was rejected
    matched: tuple = ()


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z]+", text or "")}


def _person_like(entity: str) -> bool:
    ws = entity.split()
    return 2 <= len(ws) <= 4 and not (_words(entity) & (INSTITUTION_WORDS | GEO_WORDS))


# Courtesy titles and offices that precede a name in headlines. A photo is titled "Harmanpreet Kaur", never
# "Captain Harmanpreet Kaur", so requiring them in the photo's own text would reject the right person.
ROLE_WORDS = {"captain", "skipper", "governor", "minister", "finance", "prime", "chief", "president", "vice", "union",
              "home", "defence", "defense", "external", "railway", "railways", "health", "education", "agriculture",
              "law", "foreign", "deputy", "senior", "former", "ex", "mr", "mrs", "ms", "dr", "shri", "smt", "sir",
              "justice", "judge", "general", "chairman", "chairperson", "ceo", "coach", "head", "star", "veteran",
              "actor", "actress", "singer", "director", "cm", "pm", "mp", "mla", "senator", "secretary", "speaker",
              "opposition", "leader", "legendary", "batter", "bowler", "pacer", "spinner", "all-rounder"}
_MODEL_TOKEN = re.compile(r"(?<![a-z0-9])(?=[a-z0-9]*\d)[a-z0-9]{1,8}(?![a-z0-9])")
_FULL_YEAR = re.compile(r"^(19|20)\d\d$")


def strip_roles(entity: str) -> str:
    """'Captain Harmanpreet Kaur' -> 'Harmanpreet Kaur'; 'Prime Minister Modi' -> 'Modi'. Institutions are untouched
    (their role words are not leading: 'Reserve Bank of India')."""
    words = entity.split()
    if {w.lower() for w in words} & INSTITUTION_WORDS:        # 'Law Commission', 'Health Ministry': not a person
        return entity
    rest = list(words)
    while rest and rest[0].lower().strip(".'’") in ROLE_WORDS:
        rest.pop(0)
    if len(rest) == len(words):
        return entity
    if len(rest) >= 2 or (len(rest) == 1 and len(re.sub(r"[^A-Za-z]", "", rest[0])) >= 4):
        return " ".join(rest)
    return entity


def model_tokens(text: str) -> set[str]:
    """Product/model tokens that stemming would erase: s27, a19, 17, t20, 200mp. Years are not model numbers."""
    return {m for m in _MODEL_TOKEN.findall((text or "").lower()) if not _FULL_YEAR.match(m)}


def _entity_matched(entity: str, text: str) -> bool:
    """Every distinctive word of the name - and every model number - must appear in the media's own text."""
    entity = strip_roles(entity)
    terms = query_terms(entity)
    models = model_tokens(entity)
    if not terms and not models:
        return False
    return overlap(terms, text) >= len(terms) and models <= model_tokens(text)


def _years(text: str) -> set[int]:
    return {int(y) for y in _YEAR.findall(text or "")}


def parse_date(value: str) -> datetime | None:
    m = re.search(r"(\d{4})[-:/](\d{2})[-:/](\d{2})", value or "")
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
        except ValueError:
            return None
    m = re.match(r"\s*(\d{4})\s*$", value or "")
    return datetime(int(m[1]), 1, 1, tzinfo=timezone.utc) if m else None


def different_match(text: str, story_text: str) -> str:
    """'India vs Australia 2019' is not the story 'India beat England': returns the offending fixture, or ''."""
    story_words = _words(story_text)
    for a, b in _VS.findall(text or ""):
        sides = [{w for w in _words(side) if w not in _TITLE_NOISE} for side in (a, b)]
        if all(s for s in sides) and not all(s & story_words for s in sides):
            return f"{a.strip()} vs {b.strip()}"
    return ""


def title_subject_conflict(title: str, person_entities: list[str]) -> str:
    """The media's TITLE is about a named person who is not one of the story's people (the story only gets a mention
    in the description). Returns the other person's name, or ''."""
    if not person_entities:
        return ""
    subjects = [s for s in entity_queries(title, limit=4) if _person_like(s)]
    if not subjects:
        return ""
    for s in subjects:
        if any(_entity_matched(p, s) or _entity_matched(s, p) for p in person_entities):
            return ""
    return subjects[0]


def assess(cand: dict, profile, *, query: str = "", kind: str = "photo", min_confidence: float | None = None,
           now: datetime | None = None, story_pub: datetime | None = None) -> Assessment:
    """Decide whether one candidate may be used for this story, and what it can honestly be called."""
    min_conf = config.MEDIA_MIN_CONFIDENCE if min_confidence is None else min_confidence
    now = now or datetime.now(timezone.utc)
    story_pub = story_pub or now
    title = cand.get("title", "")
    text = " ".join([title, cand.get("text", "")]).strip()
    story_text = profile.text

    if cand.get("generic_stock"):
        return Assessment(False, reason="generic stock footage is never accepted as event media")
    ents = [e for e in profile.entities if e]
    singles = [e for e in getattr(profile, "single_entities", []) if e]
    matched_strong = tuple(e for e in ents if _entity_matched(e, text))
    matched_single = tuple(e for e in singles if _entity_matched(e, text))
    ev_words = _distinct_event_words(profile)
    ev_hit = bool(ev_words) and overlap(ev_words, text) >= 1
    nongeo_single = [e for e in matched_single if e.lower() not in GEO_WORDS]
    # Evidence the media shows this story's subject: a full multi-word name, OR two of the headline's proper nouns
    # (India + England), OR one non-geographic proper noun (a brand/surname) together with the story's own event words.
    ok = bool(matched_strong) or len(matched_single) >= 2 or (len(nongeo_single) >= 1 and ev_hit)
    if not ok:
        named = ", ".join((ents + singles)[:4]) or "(none found)"
        return Assessment(False, reason=f"none of the story's named subjects ({named}) appear in the media's own "
                                        f"title/description - insufficient evidence it depicts this story")
    matched = matched_strong or matched_single
    persons = [e for e in ents if _person_like(e)]
    other = title_subject_conflict(title, persons)
    if other:
        return Assessment(False, reason=f"wrong person: the media is titled as '{other}', not the story's subject")
    fixture = different_match(text, story_text)
    if fixture:
        return Assessment(False, reason=f"different event: media shows {fixture}, which is not this story's match")
    ctx = context_terms(story_text, " ".join(matched), profile.category)
    if geo_conflict(ctx, " ".join(matched), text):
        return Assessment(False, reason="media shows a different city/state than the story")
    if tone_conflict(ctx, text):
        return Assessment(False, reason="tone mismatch (cheerful picture for a grave story)")

    # --- scoring
    ent_cov = 1.0 if matched_strong else min(1.0, len(matched_single) / 2)
    terms = query_terms(query) if query else set()
    term_cov = min(1.0, cand.get("score", 0) / len(terms)) if terms else (1.0 if cand.get("supplied_with_report") else 0.6)
    event_hit = ev_hit
    cand_dt = parse_date(cand.get("date", ""))
    cand_years = _years(title) | ({cand_dt.year} if cand_dt else set())
    stale = bool(cand_years) and max(cand_years) <= profile.event_year - 2
    recent = bool(cand_dt) and abs((story_pub - cand_dt).days) <= config.EVENT_WINDOW_DAYS
    wrong_year = bool(cand_years) and profile.years and not (cand_years & profile.years) and max(cand_years) < min(profile.years)
    permission = cand.get("permission") or "unverified"
    clear = 1.0 if permission in ALLOWED_PERMISSIONS else 0.0
    res = min(1.0, (cand.get("width", 0) or 0) / max(1, config.MEDIA_MIN_WIDTH)) if kind == "photo" else 1.0
    conf = (0.35 * min(ent_cov, 1.0) + 0.25 * term_cov + 0.15 * (1.0 if event_hit else 0.0)
            + 0.10 * (1.0 if recent or cand.get("supplied_with_report") else 0.0) + 0.10 * clear + 0.05 * res)
    conf = round(min(conf, 1.0), 2)

    # --- what can it honestly be called?
    if cand.get("supplied_with_report") and permission in ALLOWED_PERMISSIONS:
        role, why = "event", "supplied with the report by the source that holds the rights"
        conf = max(conf, 0.85)
    elif cand.get("depicts") == "event" and permission in ALLOWED_PERMISSIONS and not stale and not wrong_year:
        role, why = "event", "your rights file states it shows this event"
    elif recent and event_hit and not stale:
        role, why = "event", f"dated {cand_dt.date()}, matches the story's subject and event words"
    else:
        role = "subject"
        bits = []
        if stale or wrong_year:
            bits.append(f"dated/labelled {max(cand_years)}, older than this story")
        if not recent:
            bits.append("no date proving it is from this event")
        why = "right subject (" + ", ".join(matched[:2]) + ") but " + "; ".join(bits or ["not shown to be this event"])
        if not config.ALLOW_FILE_PHOTOS:
            return Assessment(False, conf, reason=f"only a file photo ({why}) and ALLOW_FILE_PHOTOS=false")
    if conf < min_conf:
        return Assessment(False, conf, reason=f"matching confidence {conf:.2f} below threshold {min_conf:.2f} ({why})")
    return Assessment(True, conf, role, why, matched=matched)


# ----------------------------------------------------------------------------- permissions
def _distinct_event_words(profile) -> set[str]:
    """Stems of the headline words that are not names: 'wins', 'gold', 'launch' ... the media should echo some."""
    ent_stems = {kw(w) for e in list(profile.entities) + list(getattr(profile, "single_entities", []))
                 for w in re.findall(r"[a-z]+", e.lower())}
    return {kw(w) for w in re.findall(r"[a-z]{4,}", profile.title.lower())
            if w not in STOP and w not in GENERIC_WORDS and w not in ABSTRACT_WORDS} - ent_stems


def permission_of(cand: dict, official_domains: list[str]) -> tuple[str, bool, str]:
    """(permission, attribution_required, licence label). Anything not documented is `unverified`."""
    if cand.get("permission") in ALLOWED_PERMISSIONS | {"unverified", "rejected"}:
        return cand["permission"], bool(cand.get("needs_credit", True)), cand.get("license", "")
    lic = cand.get("license", "")
    if lic == "own library":
        return "owned", bool(cand.get("needs_credit")), lic
    if official_domain(cand.get("page_url") or cand.get("url", ""), official_domains):
        return "official_permitted", True, lic or "official source"
    perm, attr = permission_for_licence(lic)
    return perm, attr, lic


def load_rights(path: Path) -> dict:
    """<video>.rights.json next to a file in VIDEO_DIR/PHOTO_DIR: the owner's statement of what the file is."""
    side = Path(str(path) + ".rights.json")
    if not side.is_file():
        side = path.with_suffix(".rights.json")
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class LocalVideos:
    """Authorised videos you placed in VIDEO_DIR, each with a .rights.json (who owns it, why you may post it, what it
    shows). A video without a rights file is never used."""
    name = "local-video"
    EXT = {".mp4", ".mov", ".m4v", ".webm"}

    def __init__(self, directory: str | Path | None = None):
        d = directory if directory is not None else config.VIDEO_DIR
        self.dir = Path(d) if d else None
        self.enabled = bool(self.dir and self.dir.is_dir())

    def video_candidates(self, query: str, context=None) -> list[dict]:
        if not self.enabled:
            return []
        terms = query_terms(query)
        out = []
        for p in sorted(self.dir.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in self.EXT:
                continue
            rights = load_rights(p)
            text = " ".join([re.sub(r"[_\-]+", " ", p.stem), str(rights.get("description", "")),
                             str(rights.get("event", ""))])
            score = overlap(terms, text) if terms else 0
            if terms and score < max(1, -(-len(terms) * 3 // 5)):
                continue
            perm = rights.get("permission", "unverified")
            out.append({"title": rights.get("title") or p.stem.replace("_", " "), "url": str(p), "score": score,
                        "text": text, "license": rights.get("licence", ""), "permission": perm if rights else "unverified",
                        "creator": rights.get("creator", ""), "page_url": rights.get("source_url", ""),
                        "date": rights.get("date", ""), "depicts": rights.get("depicts", "subject"),
                        "credit": rights.get("attribution") or rights.get("creator", ""),
                        "needs_credit": bool(rights.get("attribution") or rights.get("creator")),
                        "via": "own library", "keep_audio": bool(rights.get("keep_audio", True)),
                        "has_rights_file": bool(rights)})
        return out

    def download(self, url: str, dest: Path) -> Path:
        shutil.copyfile(url, dest)
        return dest


class CommonsVideos(CommonsClient):
    """Openly licensed VIDEO from Wikimedia Commons (webm/ogv). Same licence rules as photos."""
    VIDEO_MIME = {"video/webm", "application/ogg", "video/ogg", "video/mp4"}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.name = "commons-video"

    def video_candidates(self, query: str, context=None) -> list[dict]:
        terms = query_terms(query)
        out = []
        for page in self.search(query, filetype="video"):
            info = (page.get("imageinfo") or [{}])[0]
            if info.get("mime") not in self.VIDEO_MIME or not info.get("url"):
                continue
            lic = _meta(info, "LicenseShortName")
            if not license_ok(lic) or _meta(info, "NonFree").lower() == "true":
                continue
            title = re.sub(r"\.\w{3,4}$", "", re.sub(r"^File:", "", page.get("title", ""))).replace("_", " ")
            text = " ".join([title, _meta(info, "ImageDescription"), _meta(info, "Categories").replace("|", " ")])
            score = overlap(terms, text)
            if score < max(1, -(-len(terms) * 3 // 5)):
                continue
            artist = _meta(info, "Artist")
            out.append({"title": title, "url": info["url"], "score": score, "text": text, "license": lic,
                        "creator": artist, "date": _meta(info, "DateTimeOriginal") or _meta(info, "DateTime"),
                        "credit": f"{(artist or 'Unknown author')[:60]} ({lic})", "needs_credit": needs_attribution(lic),
                        "via": "Wikimedia Commons", "page_url": info.get("descriptionurl", ""),
                        "width": info.get("width") or 0, "height": info.get("height") or 0})
        return out


# ----------------------------------------------------------------------------- polite downloads
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def robots_allows(url: str, session=None) -> bool:
    """Honour robots.txt for direct file fetches from publisher hosts. An unreadable robots.txt is treated as 'allowed'
    (the media still needs a documented reuse permission to get this far)."""
    parts = urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}"
    if root not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = (session or requests).get(root + "/robots.txt", headers=_UA, timeout=8)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            _robots[root] = rp
        except Exception:  # noqa: BLE001
            _robots[root] = None
    rp = _robots[root]
    return True if rp is None else rp.can_fetch(_UA["User-Agent"], url)


def fetch_file(url: str, dest: Path, *, max_bytes: int, session=None, check_robots: bool = False) -> Path:
    http = session or requests
    if check_robots and not robots_allows(url, session):
        raise MediaRejected("robots.txt disallows fetching this file")
    r = http.get(url, headers=_UA, timeout=90, stream=True)
    if r.status_code != 200:
        raise MediaRejected(f"download HTTP {r.status_code}")
    size = 0
    with open(dest, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            size += len(chunk)
            if size > max_bytes:
                raise MediaRejected("file larger than the size limit")
            f.write(chunk)
    return dest


# ----------------------------------------------------------------------------- retrieval
class MediaRetriever:
    """Finds, vets and downloads story-specific media. Every dependency is injectable so tests never touch the network."""

    def __init__(self, *, photo_sources: dict | None = None, video_sources: dict | None = None,
                 official_domains: list[str] | None = None, session=None, stock_available: bool | None = None,
                 min_confidence: float | None = None, max_photos: int = 6):
        self.photo_sources = photo_sources if photo_sources is not None else self._default_photo_sources()
        self.video_sources = video_sources if video_sources is not None else self._default_video_sources()
        self.official_domains = official_domains if official_domains is not None else _official_domains()
        self.session = session
        self.stock_available = stock_available
        self.min_confidence = min_confidence
        self.max_photos = max_photos

    @staticmethod
    def _default_photo_sources() -> dict:
        out: dict = {}
        if config.PHOTO_DIR:
            out["local"] = LocalPhotos()
        if "commons" in config.PHOTO_SOURCES:
            out["commons"] = CommonsClient()
        if "openverse" in config.PHOTO_SOURCES:
            out["openverse"] = OpenverseClient()
        return out

    @staticmethod
    def _default_video_sources() -> dict:
        out: dict = {}
        if config.VIDEO_DIR:
            out["local"] = LocalVideos()
        if config.COMMONS_VIDEO and "commons" in config.PHOTO_SOURCES:
            out["commons"] = CommonsVideos()
        return out

    # -- story queries
    def queries_for(self, cluster: dict, profile, content: dict | None = None) -> list[str]:
        story_text = " ".join([cluster["lead"].get("title", ""), cluster["lead"].get("summary", "")[:400]])
        qs: list[str] = []
        singles = list(getattr(profile, "single_entities", []))
        combos = [" ".join(singles[:2])] if len(singles) >= 2 else []
        combos += [x for x in singles if x.lower() not in GEO_WORDS][:2]
        for q in profile.entities + combos + photo_queries_for(story_text, (content or {}).get("photo_queries")):
            q = " ".join(q.split())
            if q and q.lower() not in {x.lower() for x in qs}:
                qs.append(q)
        return qs[:6]

    # -- main entry
    def retrieve(self, cluster: dict, profile, work_dir: Path, content: dict | None = None,
                 now: datetime | None = None) -> MediaPlan:
        now = now or datetime.now(timezone.utc)
        work_dir = Path(work_dir)
        (work_dir / "media").mkdir(parents=True, exist_ok=True)
        plan = MediaPlan(story_id=profile.story_id)
        plan.queries = self.queries_for(cluster, profile, content)
        pubs = [datetime.fromisoformat(a["published"]) for a in cluster["articles"] if a.get("published")]
        story_pub = min(pubs) if pubs else now
        seen_hashes: list[int] = []
        seen_sha: set[str] = set()
        counter = {"n": 0}
        tried: set[tuple[str, str]] = set()          # one verdict per file, however many queries surface it

        def consider(cand: dict, source: str, kind: str, query: str, downloader) -> None:
            perm, attr, lic = permission_of(cand, self.official_domains)
            cand = {**cand, "permission": perm}
            url = cand.get("url", "")
            if (kind, url) in tried:
                return
            tried.add((kind, url))
            if perm not in ALLOWED_PERMISSIONS:
                plan.rejected.append(Rejection(cand.get("title", "")[:80], source,
                                               "no documented permission to reuse (" + (lic or perm) + ") - not downloaded",
                                               kind, url))
                return
            a = assess(cand, profile, query=query, kind=kind, min_confidence=self.min_confidence, now=now,
                       story_pub=story_pub)
            if not a.accepted:
                plan.rejected.append(Rejection(cand.get("title", "")[:80], source, a.reason, kind, url))
                log.info("Rejected %s candidate '%s' [%s]: %s", kind, cand.get("title", "")[:50], source, a.reason)
                return
            counter["n"] += 1
            dest = work_dir / "media" / f"{kind}_{counter['n']:02d}{'.mp4' if kind == 'video' else '.jpg'}"
            try:
                if kind == "photo":
                    downloader(url, dest)
                    w, h, ph = validate_image_file(dest, min_width=config.MEDIA_MIN_WIDTH,
                                                   min_height=config.MEDIA_MIN_HEIGHT)
                    if is_duplicate_image(ph, seen_hashes):
                        raise MediaRejected("duplicate of a picture already selected")
                    seen_hashes.append(ph)
                    dur, audio, width, height = 0.0, False, w, h
                else:
                    downloader(url, dest)
                    width, height, dur, audio = _validate_video(dest)
                    ph = 0
                sha = file_sha256(dest)
                if sha in seen_sha:
                    raise MediaRejected("duplicate file")
                seen_sha.add(sha)
            except (MediaRejected, OSError, RuntimeError, requests.RequestException) as e:
                reason = getattr(e, "reason", None) or mask_secrets(f"{type(e).__name__}: {e}")[:140]
                plan.rejected.append(Rejection(cand.get("title", "")[:80], source, reason, kind, url))
                dest.unlink(missing_ok=True)
                return
            credit = cand.get("credit") or cand.get("creator") or ""
            asset = MediaAsset(
                kind=kind, source=source, title=cand.get("title", ""), original_url=url,
                page_url=cand.get("page_url", ""), creator=cand.get("creator", ""), license=lic, permission=perm,
                attribution=credit, attribution_required=attr, caption_credit=cand.get("caption_credit", ""),
                role=a.role, confidence=a.confidence, rationale=a.rationale, story_id=profile.story_id,
                retrieved_at=now_iso(), media_date=cand.get("date", ""), width=width, height=height, duration=dur,
                path=str(dest), sha256=sha, phash=ph, has_audio=audio and cand.get("keep_audio", True))
            (plan.videos if kind == "video" else plan.photos).append(asset)
            log.info("Accepted %s [%s/%s conf %.2f]: %s", kind, a.role, perm, a.confidence, asset.title[:60])

        for provider in config.MEDIA_SOURCE_ORDER:
            if provider == "official":
                try:
                    plan.providers["official"] = self._official(cluster, plan, consider, now)
                except Exception as e:  # noqa: BLE001
                    plan.providers["official"] = f"error: {type(e).__name__}"
                continue
            psrc, vsrc = self.photo_sources.get(provider), self.video_sources.get(provider)
            if psrc is None and vsrc is None:
                plan.providers[provider] = "skipped: not configured"
                continue
            status = "ok"
            for q in plan.queries:
                if len(plan.photos) >= self.max_photos and (vsrc is None or plan.videos):
                    break
                if psrc is not None and getattr(psrc, "enabled", True) and len(plan.photos) < self.max_photos:
                    try:
                        for c in psrc.candidates(q, context_terms(profile.text, q, profile.category))[:8]:
                            if len(plan.photos) >= self.max_photos:
                                break
                            consider(c, provider, "photo", q, psrc.download)
                    except Exception as e:  # noqa: BLE001
                        status = f"error: {type(e).__name__}"
                        log.warning("%s photo search '%s' failed: %s", provider, q, mask_secrets(str(e))[:100])
                if vsrc is not None and getattr(vsrc, "enabled", True) and len(plan.videos) < 2:
                    try:
                        for c in vsrc.video_candidates(q)[:6]:
                            consider(c, provider + "-video", "video", q, vsrc.download)
                    except Exception as e:  # noqa: BLE001
                        status = f"error: {type(e).__name__}"
                        log.warning("%s video search '%s' failed: %s", provider, q, mask_secrets(str(e))[:100])
            plan.providers[provider] = status
        plan.photos.sort(key=lambda a: (a.role != "event", -a.confidence))
        plan.videos.sort(key=lambda a: (a.role != "event", -a.confidence))
        plan.illustrative_clips = self._illustrative(cluster)
        return plan

    # -- publisher / official media attached to the articles themselves
    def _official(self, cluster: dict, plan: MediaPlan, consider, now) -> str:
        n = 0
        for art in cluster["articles"][:6]:
            for m in art.get("media", []) or []:
                n += 1
                hint = m.get("licence_hint", "")
                lic = licence_from_url(hint) or ("" if "creativecommons" in hint.lower() else hint)
                cand = {"title": art["title"], "url": m["url"], "score": 0, "text": art["title"] + " " + art.get("summary", ""),
                        "width": m.get("width", 0), "height": m.get("height", 0), "license": lic,
                        "creator": m.get("credit") or art["source"], "credit": m.get("credit") or art["source"],
                        "date": art.get("published") or "", "page_url": art["url"], "supplied_with_report": True,
                        "needs_credit": True, "via": art["source"]}
                if official_domain(art["url"], self.official_domains):
                    cand["permission"] = "official_permitted"
                kind = m["kind"]
                consider(cand, "official", kind, "", lambda u, d, k=kind: fetch_file(
                    u, d, max_bytes=MAX_VIDEO_BYTES if k == "video" else 25 * 1024 * 1024, session=self.session,
                    check_robots=True))
        return f"ok ({n} linked media item(s) inspected)" if n else "ok (no media linked by the articles)"

    def _illustrative(self, cluster: dict) -> int:
        if self.stock_available is not None:
            return 1 if self.stock_available else 0
        try:
            from .stock import configured_providers
            return 1 if (configured_providers() or config.BROLL_DIR) else 0
        except Exception:  # noqa: BLE001
            return 0


def _official_domains() -> list[str]:
    from .feeds import load_sources_file
    extra = [str(d).lower() for d in (load_sources_file(config.SOURCES_FILE).get("official_media_domains") or [])]
    return list(dict.fromkeys(config.OFFICIAL_MEDIA_DOMAINS + extra))


def _validate_video(path: Path) -> tuple[int, int, float, bool]:
    """Probe a downloaded video for real. Returns (w, h, seconds, has_audio) or raises MediaRejected."""
    from .video_renderer import VideoError, probe
    try:
        info = probe(path)
    except VideoError as e:
        raise MediaRejected(f"cannot inspect video ({e})") from e
    if not info.get("video") or not info.get("size"):
        raise MediaRejected("file has no decodable video stream")
    dur = float(info.get("duration") or 0)
    if dur < config.VIDEO_MIN_SECONDS:
        raise MediaRejected(f"video shorter than {config.VIDEO_MIN_SECONDS:.0f}s")
    w, h = info["size"]
    if min(w, h) < 360:
        raise MediaRejected(f"video resolution {w}x{h} too low")
    return w, h, dur, bool(info.get("audio"))
