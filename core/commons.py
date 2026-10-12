"""Real documentary photographs from Wikimedia Commons (no API key).

Why Commons: it hosts millions of REAL photos of people, places, institutions and events under free licences,
with machine-readable licence + author data, so every photo can be credited correctly.

Only licences that allow commercial reuse of a video WITHOUT share-alike obligations are accepted:
CC0, Public Domain and CC BY (any version). CC BY-SA / NC / ND, GFDL, "non-free" and trademarked/logo files are
rejected. A photo is used only if its title/description/categories match the search term (named people, places,
institutions). Credits are returned so they can be shown on screen and in the caption (required by CC BY).
"""
from __future__ import annotations

import html
import logging
import math
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from . import config
from .health import mask_secrets
from .dedupe import STOP
from .stock import ABSTRACT_WORDS, GENERIC_WORDS, kw, overlap, query_terms
from .storage import prune_dict_by_age, read_json, write_json

log = logging.getLogger("commons")
API = "https://commons.wikimedia.org/w/api.php"
CACHE_HOURS = 24
MAX_BYTES = 20 * 1024 * 1024
REJECT_TITLE_WORDS = {"map", "maps", "flag", "flags", "logo", "logos", "coat", "arms", "locator", "diagram", "chart",
                      "screenshot", "icon", "emblem", "signature", "seal", "stamp", "banknote", "coin", "poster",
                      "cover", "wordmark", "infographic", "graph", "svg", "scan", "document", "certificate"}


@dataclass
class Photo:
    path: Path
    credit: str          # "Jane Doe (CC BY 4.0)"
    title: str
    query: str
    via: str = "Wikimedia Commons"
    needs_credit: bool = True      # show the credit on screen (CC BY / GODL); CC0 and Public Domain need none
    caption_credit: str = ""       # caption wording incl. a source URL where the licence requires it (GODL-India)


INSTITUTION_WORDS = {"commission", "court", "stadium", "gardens", "garden", "university", "institute", "bank", "parliament",
                     "ministry", "assembly", "council", "hospital", "school", "college", "league", "cup", "museum",
                     "palace", "temple", "fort", "airport", "station", "bridge", "secretariat", "headquarters", "tribunal",
                     "party", "supreme", "union", "government"}
GEO_WORDS = {"india", "bengal", "delhi", "mumbai", "kolkata", "chennai", "bengaluru", "bangalore", "hyderabad", "pune",
             "kerala", "punjab", "gujarat", "rajasthan", "maharashtra", "karnataka", "tamil", "nadu", "odisha", "assam",
             "bihar", "uttar", "pradesh", "china", "pakistan", "korea", "japan", "america", "england", "australia", "london",
             "york", "paris", "dubai", "singapore", "europe", "africa", "asia", "tokyo", "beijing", "washington", "moscow",
             "russia", "ukraine", "israel", "gaza", "iran", "germany", "france", "canada", "nepal", "bangladesh", "lanka",
             "bhutan", "city", "state", "district", "road", "street", "market", "river", "national", "park"}

CONTEXT_LEXICON = {
    "Entertainment": {"film", "movie", "cinema", "actor", "actress", "star", "director", "music", "song", "album", "singer",
                      "concert", "fashion", "designer", "couture", "show", "series", "festival", "producer", "musician"},
    "Sports": {"cricket", "football", "match", "stadium", "team", "player", "tournament", "league", "coach", "captain",
               "batsman", "bowler", "athlete", "championship", "olympic", "tennis", "badminton", "goal"},
    "News": {"election", "minister", "government", "parliament", "court", "police", "voter", "party", "rally", "assembly",
             "leader", "politician", "chief", "official", "campaign", "protest", "mayor"},
    "Technology": {"technology", "software", "company", "startup", "chief", "executive", "launch", "device", "engineer",
                   "founder", "research", "developer", "computer", "mobile", "robot"},
}


_COUNTRIES = {"india", "china", "pakistan", "korea", "japan", "america", "england", "australia", "europe", "africa", "asia",
              "russia", "ukraine", "israel", "gaza", "iran", "germany", "france", "canada", "nepal", "bangladesh", "lanka",
              "bhutan"}
_GENERIC_GEO = {"city", "state", "district", "road", "street", "market", "river", "national", "park", "tamil", "nadu",
                "uttar", "pradesh"}
# Cities / states: a photo taken in a DIFFERENT one of these is the wrong picture ("Jantar Mantar at Jaipur" for a Delhi story)
SPECIFIC_PLACES = (GEO_WORDS - _COUNTRIES - _GENERIC_GEO) | {
    "jaipur", "lucknow", "ahmedabad", "patna", "bhopal", "kanpur", "varanasi", "amritsar", "chandigarh", "jodhpur", "udaipur",
    "indore", "nagpur", "surat", "ranchi", "guwahati", "kochi", "chennai", "madurai", "mysuru", "mysore", "goa", "shimla",
    "dehradun", "srinagar", "jammu", "kashmir", "ladakh", "manipur", "haryana", "jharkhand", "telangana", "tripura"}


def _places(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z]+", text or "")} & SPECIFIC_PLACES


GRIM_WORDS = {"earthquake", "tsunami", "flood", "floods", "disaster", "killed", "dead", "death", "deaths", "crash", "attack",
              "blast", "explosion", "war", "shooting", "fire", "collapse", "casualties", "victims", "bombing"}
CHEERFUL_WORDS = {"rainbow", "sunset", "sunrise", "party", "wedding", "festival", "smiling", "selfie", "vacation", "tourist",
                  "tourists", "cute", "beautiful", "scenic", "holiday", "celebration"}


def tone_conflict(context: set[str] | None, text: str) -> bool:
    """A cheerful picture (rainbow, sunset, party) must not illustrate a disaster or an attack."""
    return bool(context and "tone:grim" in context
                and {w.lower() for w in re.findall(r"[A-Za-z]+", text or "")} & CHEERFUL_WORDS)


def geo_conflict(context: set[str] | None, query: str, text: str) -> bool:
    """True when the story is set in one city/state and the photo shows another (and the search term did not ask for it)."""
    story = {t[4:] for t in (context or ()) if t.startswith("geo:")}
    shown = _places(text) - _places(query)
    return bool(story and shown and not (story & shown))


def context_threshold(query: str) -> int:
    """How many story-topic words a photo's text must contain:
    0 for institutions/landmarks (Election Commission of India, Eden Gardens: the name itself is the subject),
    2 for countries/states/cities (a generic street photo must never stand in for a political story),
    1 for people (same-name check: a surgeon is not a fashion designer)."""
    words = {w.lower() for w in re.findall(r"[A-Za-z]+", query)}
    if words & INSTITUTION_WORDS:
        return 0
    if words & GEO_WORDS:
        return 2
    return 1


def needs_context(query: str) -> bool:
    return context_threshold(query) > 0


def context_ok(query: str, context: set[str] | None, text: str) -> bool:
    if geo_conflict(context, query, text) or tone_conflict(context, text):
        return False
    thr = context_threshold(query)
    if not context or thr == 0:
        return True
    if thr >= 2:   # a country/city name alone proves nothing: the photo must share words with THIS story
        story = {t[3:] for t in context if t.startswith("sw:")}
        return overlap(story, text) >= thr if story else overlap(context, text) >= thr
    if len(re.findall(r"[A-Za-z]+", query)) == 1:   # "Panama", "Tesla": one word could be a country, a brand or a person
        story = {t[3:] for t in context if t.startswith("sw:")}
        return overlap(story, text) >= 2 if story else overlap(context, text) >= thr
    return overlap(context, text) >= thr


def context_terms(story_text: str, query: str, category: str = "News") -> set[str]:
    """Topic words (not names) that a photo of the RIGHT subject should mention: story words + category lexicon."""
    qstems = {kw(w) for w in re.findall(r"[a-z]+", query.lower())}
    words = [w for w in re.findall(r"\b[a-z]{4,}\b", story_text) if w not in STOP and w not in GENERIC_WORDS
             and w not in ABSTRACT_WORDS]
    story = {kw(w) for w in words} - qstems
    stems = ({kw(w) for w in words} | {kw(w) for w in CONTEXT_LEXICON.get(category, set())}) - qstems
    marks = {f"geo:{p}" for p in _places(story_text)} | {f"sw:{w}" for w in story}   # sw: = words from THIS story only
    if {w.lower() for w in re.findall(r"[A-Za-z]+", story_text)} & GRIM_WORDS:
        marks.add("tone:grim")
    return stems | marks


def license_ok(short: str) -> bool:
    """CC0 / Public Domain / CC BY / GODL-India only. Rejects share-alike, non-commercial, no-derivatives, GFDL, fair use."""
    s = (short or "").lower().replace("-", " ").replace("_", " ").strip()
    if not s:
        return False
    if set(s.split()) & {"sa", "nc", "nd", "gfdl"} or "share" in s or "non free" in s or "fair use" in s:
        return False
    if s.startswith(("cc0", "public domain")) or s == "pd" or s.startswith("pd "):
        return True
    if s.startswith("godl") or "government open data licen" in s:
        return True   # Government Open Data License - India: commercial use + adaptation allowed with attribution
    return bool(re.match(r"cc by( \d|$)", s))


def needs_attribution(short: str) -> bool:
    """CC0 / Public Domain need no credit; CC BY and GODL-India do (we show it on screen and in the caption)."""
    s = (short or "").lower().replace("-", " ").strip()
    return not (s.startswith(("cc0", "public domain")) or s == "pd" or s.startswith("pd "))


def _strip_html(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value or "")).split())


def _meta(info: dict, key: str) -> str:
    return _strip_html(((info.get("extmetadata") or {}).get(key) or {}).get("value", ""))


_LEAD_STOP = {"the", "this", "that", "these", "those", "a", "an", "in", "on", "at", "as", "after", "before", "how", "why",
              "what", "when", "where", "who", "new", "over", "with", "for", "from", "by", "to", "of", "and", "its", "his",
              "her", "their", "our", "your", "amid", "says", "said"}
_CONNECT = {"of", "the", "de", "for"}   # not "and": it would glue two separate people into one name
# Title-Case headlines capitalise every word ("Delhi Is", "Opposition Alleges Undeclared Emergency"). A real name never
# contains a function word or a headline verb, so such runs are not searched for as people/places.
_HEADLINE_VERBS = set("""acquires acquire alleges alleged targets target slams slam hits hit wins win beats beat launches
launch unveils unveil announces announce reveals reveal sets set gets get takes take makes make faces face plans plan seeks
seek urges urge warns warn claims claim backs back joins join quits quit ends end begins begin opens open closes close
drops drop rises rise falls fall shows show finds find leads lead holds hold puts put sees see expects expect wants want
needs need becomes become remains remain appears appear confirms confirm denies deny rejects reject approves approve
orders order signs sign signals signal sparks spark triggers trigger hails hail blasts blast slashes slash cuts cut hikes
hike boosts boost eyes eye mulls mull calls call says asks ask tells tell gives give brings bring puts moves move turns turn
ahead against amid after before during since until while starring stars featuring directed produced led""".split())


def _real_name(words: list[str]) -> bool:
    inner = [w.lower() for w in words if w.lower() not in _CONNECT]
    return len(words) <= 5 and not any(w in STOP or w in _HEADLINE_VERBS for w in inner)


def entity_queries(text: str, limit: int = 4) -> list[str]:
    """Multi-word proper names (people, places, institutions): runs of Capitalised words, e.g. 'West Bengal',
    'Election Commission of India', 'Trent Reznor'."""
    toks = [t.strip("'’-") or t for t in re.findall(r"[A-Za-z][A-Za-z0-9'’-]*|\d[\d,.]*|[.,;:!?()\"“”‘’—–|/]", text or "")]
    out: list[str] = []
    cur: list[str] = []

    def add(run: list[str]) -> None:
        while run and run[0].lower() in _LEAD_STOP:   # drop sentence-initial filler ("The", "After" ...)
            run.pop(0)
        while run and run[-1].lower() in _CONNECT:
            run.pop()
        if len(run) >= 2 and _real_name(run):
            name = " ".join(run)
            if name.lower() not in {x.lower() for x in out}:
                out.append(name)

    def flush() -> None:
        run: list[str] = []
        for w in cur:          # a Title-Case headline run is cut at verbs/function words: "Acquires Rom Com Starring ..."
            if (w.lower() in STOP and w.lower() not in _CONNECT) or w.lower() in _HEADLINE_VERBS:
                add(run)
                run = []
            else:
                run.append(w)
        add(run)
        cur.clear()

    for i, t in enumerate(toks):
        if t[0].isalpha() and (t[0].isupper() or re.match(r"[a-z]+[A-Z]", t)):    # Samsung, S27, and camel-case iPhone
            cur.append(t)
        elif (t.isdigit() and len(t) <= 3 and cur and i + 1 < len(toks) and toks[i + 1][:1].isupper()
              and toks[i + 1][:1].isalpha()):                                       # "iPhone 17 Pro Max": the model number is part of the name
            cur.append(t)
        elif t.lower() in _CONNECT and cur and i + 1 < len(toks) and toks[i + 1][:1].isupper():
            cur.append(t.lower())
        else:
            flush()
    flush()
    return out[:limit]


def photo_queries_for(story_text: str, ai_queries: list[str] | None) -> list[str]:
    out: list[str] = []
    for q in list(ai_queries or []) + entity_queries(story_text):
        q = " ".join(re.sub(r"[^A-Za-z0-9' ]", " ", q or "").split()[:5])
        if q and q.lower() not in {x.lower() for x in out}:
            out.append(q)
    return out[:6]


class PhotoSource:
    """Shared download + selection logic for open-licence photo providers."""
    name = ""
    enabled = True
    http = requests

    @property
    def headers(self) -> dict:
        return {"User-Agent": f"InstaNewsAgent/1.0 (news reels; Instagram {config.ACCOUNT_HANDLE}) python-requests"}

    def candidates(self, query: str, context: set[str] | None = None) -> list[dict]:
        raise NotImplementedError

    def download(self, url: str, dest: Path) -> Path:
        r = self.http.get(url, headers=self.headers, timeout=90, stream=True)
        if r.status_code != 200:
            raise RuntimeError(f"download HTTP {r.status_code}")
        size = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise RuntimeError("photo too large")
                f.write(chunk)
        return dest

    # ------------------------------------------------------------------ public
    def fetch_photos(self, queries: list[str], out_dir: Path, wanted: int = 4, per_query: int = 2,
                     rng: random.Random | None = None, context: set[str] | None = None) -> list[Photo]:
        rng = rng or random.Random()
        out_dir.mkdir(parents=True, exist_ok=True)
        got: list[Photo] = []
        seen: set[str] = set()
        for q in queries:
            if len(got) >= wanted:
                break
            try:
                cands = [c for c in self.candidates(q, context) if c["title"] not in seen]
            except Exception as e:  # noqa: BLE001 - Commons outage must never fail a Reel
                log.warning("%s search '%s' failed: %s", self.name, q, mask_secrets(str(e))[:100])
                continue
            if not cands:
                log.info("%s: no licence-safe relevant photo for '%s'", self.name, q)
                continue
            best = max(c["score"] for c in cands)
            pool = [c for c in cands if c["score"] == best][:6]
            rng.shuffle(pool)
            taken = 0
            for c in pool:
                if taken >= per_query or len(got) >= wanted:
                    break
                try:
                    path = self.download(c["url"], out_dir / f"{self.name}_{len(got):02d}.jpg")
                except Exception as e:  # noqa: BLE001
                    log.warning("Photo download failed: %s", mask_secrets(str(e))[:100])
                    continue
                seen.add(c["title"])
                got.append(Photo(path, c["credit"], c["title"], q, c.get("via", ""), c.get("needs_credit", True),
                                 c.get("caption_credit", "")))
                taken += 1
                log.info("Photo for '%s': %s [%s]", q, c["title"][:60], c["credit"])
            time.sleep(0.2)  # be polite to the Wikimedia API
        return got


class CommonsClient(PhotoSource):
    def __init__(self, session=None, cache_path: Path | None = None, enabled: bool = True):
        self.http = session or requests
        self.cache_path = cache_path or (config.DATA_DIR / "commons_cache.json")
        self.enabled = enabled
        self.name = "commons"

    # ------------------------------------------------------------------ search
    def search(self, query: str, limit: int = 20, filetype: str = "bitmap") -> list[dict]:
        cache = read_json(self.cache_path, {})
        ckey = f"q:{query}" if filetype == "bitmap" else f"q:{filetype}:{query}"
        hit = cache.get(ckey)
        if hit and time.time() - hit["ts"] < CACHE_HOURS * 3600:
            return hit["pages"]
        r = self.http.get(API, headers=self.headers, timeout=25, params={
            "action": "query", "format": "json", "formatversion": 2, "generator": "search",
            "gsrsearch": f"{query} filetype:{filetype}", "gsrnamespace": 6, "gsrlimit": limit,
            "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1600,
            "iiextmetadatafilter": "Artist|Attribution|LicenseShortName|NonFree|Restrictions|ImageDescription|ObjectName|Categories|DateTimeOriginal|DateTime"})
        if r.status_code != 200:
            raise RuntimeError(f"Commons HTTP {r.status_code}")
        pages = sorted((r.json().get("query") or {}).get("pages", []) or [], key=lambda p: p.get("index", 999))
        slim = [{"title": p.get("title", ""), "pageid": p.get("pageid"), "index": p.get("index", 999),
                 "imageinfo": p.get("imageinfo", [])[:1]}
                for p in pages]
        cache[ckey] = {"ts": time.time(), "pages": slim}
        write_json(self.cache_path, prune_dict_by_age(cache, max_entries=120, max_age_hours=CACHE_HOURS))
        return slim

    def candidates(self, query: str, context: set[str] | None = None) -> list[dict]:
        """Licence-safe, relevant, usable photos for a search term, best first. For person-like names the photo's
        text must also mention the story's topic (a surgeon named like a fashion designer must not match)."""
        terms = query_terms(query)
        need = max(1, math.ceil(0.6 * len(terms)))
        qwords = {w.lower() for w in re.findall(r"[A-Za-z]+", query)}
        out = []
        for page in self.search(query):
            info = (page.get("imageinfo") or [{}])[0]
            title = re.sub(r"^File:", "", page.get("title", ""))
            title = re.sub(r"\.\w{3,4}$", "", title).replace("_", " ")
            if info.get("mime") not in ("image/jpeg", "image/png") or (info.get("width") or 0) < 1000:
                continue
            if _meta(info, "NonFree").lower() == "true" or "trademark" in _meta(info, "Restrictions").lower():
                continue
            lic = _meta(info, "LicenseShortName")
            if not license_ok(lic):
                continue
            if {w.lower() for w in re.findall(r"[A-Za-z]+", title)} & (REJECT_TITLE_WORDS - qwords):
                continue
            text = " ".join([title, _meta(info, "ImageDescription"), _meta(info, "ObjectName"),
                             _meta(info, "Categories").replace("|", " ")])
            score = overlap(terms, text)
            if score < need:
                continue
            if not context_ok(query, context, text):
                continue
            artist = _meta(info, "Artist")
            if not artist or re.match(r"(?i)institution\s+QS:", artist):   # raw Wikidata placeholder, not a name
                artist = ""
            if lic.lower().replace("-", " ").startswith("godl"):
                attribution = _meta(info, "Attribution")
                credit = attribution if "godl" in attribution.lower() else f"{(attribution or artist or 'Government of India')[:60]} (GODL-India)"
                pid = page.get("pageid")
                caption_credit = f"{credit}, commons.wikimedia.org/?curid={pid}" if pid else credit   # GODL requires a source URL
            else:
                credit = f"{(artist or 'Unknown author')[:60]} ({lic})"
                caption_credit = ""
            out.append({"title": title, "url": info.get("thumburl") or info.get("url"), "score": score,
                        "width": info.get("width") or 0, "height": info.get("height") or 0, "credit": credit,
                        "caption_credit": caption_credit, "needs_credit": needs_attribution(lic),
                        "via": "Wikimedia Commons", "license": lic, "creator": artist, "text": text,
                        "page_url": info.get("descriptionurl") or (f"https://commons.wikimedia.org/?curid={page.get('pageid')}"
                                                                    if page.get("pageid") else ""),
                        "date": _meta(info, "DateTimeOriginal") or _meta(info, "DateTime")})
        out.sort(key=lambda c: (-c["score"], -min(c["width"], 3000)))
        return out



def images_credit(photos: list[Photo], max_items: int = 3) -> str | None:
    if not photos:
        return None
    seen, parts, vias = set(), [], []
    for p in photos:
        line = p.caption_credit or p.credit
        if line not in seen:
            seen.add(line)
            parts.append(line)
        if p.via and p.via not in vias:
            vias.append(p.via)
    extra = f" and {len(parts) - max_items} more" if len(parts) > max_items else ""
    return ", ".join(parts[:max_items]) + extra + (" via " + ", ".join(vias) if vias else "")
