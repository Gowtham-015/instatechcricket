"""Background footage for Reels. Providers are tried in order (STOCK_PROVIDER=auto):

  pexels   free API key (new key issuance may be paused)          portrait clips
  pixabay  free API key https://pixabay.com/api/docs/             mostly landscape -> centre-cropped to 9:16
  local    YOUR OWN clips in BROLL_DIR (optionally per-category sub-folders)  <- needs no account/API at all
  (none)   animated background

Licences: Pexels and Pixabay content is free for commercial use without attribution (we still add a small
caption credit). Footage is illustrative b-roll chosen from generic search terms, never presented as footage
of the actual event.
"""
from __future__ import annotations

import logging
import math
import random
import re
import time
from pathlib import Path

import requests

from . import config
from .health import mask_secrets
from .storage import prune_dict_by_age, read_json, write_json

log = logging.getLogger("stock")
PEXELS_API = "https://api.pexels.com/videos/search"
PIXABAY_API = "https://pixabay.com/api/videos/"
MAX_BYTES = 80 * 1024 * 1024
CACHE_HOURS = 24  # Pixabay requires API responses to be cached for 24 h

CATEGORY_BROLL = {
    "News": ["city street traffic", "crowd walking city", "newsroom", "government building"],
    "Technology": ["technology circuit board", "typing on laptop", "data center servers", "futuristic technology"],
    "Sports": ["stadium crowd", "sports training", "cricket stadium", "athlete running"],
    "Entertainment": ["cinema screen", "concert stage lights", "film camera", "red carpet"],
}


GENERIC_WORDS = {"video", "videos", "footage", "people", "person", "scene", "background", "stock", "view", "shot",
                 "clip", "man", "woman", "new", "day", "the", "and", "for", "with", "free"}
ABSTRACT_WORDS = {"issue", "issues", "appeal", "appeals", "pending", "functional", "vacant", "policy", "crisis",
                  "concept", "idea", "trend", "trends", "update", "updates", "news", "story", "debate", "tension",
                  "development", "situation", "impact", "reason", "reasons", "result", "results", "plan", "plans"}
# Tags that mean the clip is about something else entirely (animals, insects, close-ups of shoes) unless the search asked for it.
OFF_TOPIC_TAGS = {"frog", "frogs", "insect", "insects", "butterfly", "bird", "birds", "fish", "flower", "flowers", "animal",
                  "animals", "wildlife", "pet", "pets", "puppy", "kitten", "toy", "toys", "cartoon", "feet", "shoes", "shoe",
                  "boots", "sandals", "legs", "baby"}
_SUFFIXES = ("ions", "ion", "ing", "ers", "er", "ed", "es", "al", "ly", "s", "e")


def kw(word: str) -> str:
    """Tiny stemmer: voting/voters/vote -> 'vot'. Used to compare query words with clip tags."""
    w = word.lower()
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            break
    return w[:4]


def clean_query(q: str) -> str:
    words = [w for w in re.sub(r"[^a-zA-Z ]", " ", q or "").lower().split() if w not in ABSTRACT_WORDS]
    return " ".join(words[:5])


def query_terms(q: str) -> set[str]:
    return {kw(w) for w in re.findall(r"[a-z]+", q.lower()) if len(w) >= 3 and w not in GENERIC_WORDS}


def overlap(terms: set[str], text: str) -> int:
    have = {kw(w) for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 3}
    return len(terms & have)


# Concrete, filmable scenes for common news topics (searched BEFORE the AI's own suggestions).
TOPIC_VISUALS: list[tuple[set[str], list[str]]] = [
    ({"election", "elections", "bypoll", "bypolls", "vote", "voters", "voting", "ballot", "poll", "polls", "candidate",
      "campaign", "lok", "sabha", "assembly", "electoral"},
     ["voting ballot box", "people voting queue", "election rally crowd", "indian flag waving"]),
    ({"court", "tribunal", "tribunals", "judge", "verdict", "petition", "supreme", "hearing", "lawsuit", "bail",
      "trial", "justice", "judiciary", "appeal", "appeals"},
     ["courtroom gavel", "legal documents desk", "law books library", "justice statue"]),
    ({"parliament", "minister", "government", "cabinet", "bill", "lawmakers", "govt", "legislation", "policy", "pm"},
     ["parliament building", "government office", "signing documents", "indian flag waving"]),
    ({"stock", "stocks", "sensex", "nifty", "market", "markets", "shares", "investors", "inflation", "rupee", "economy",
      "gdp", "rbi", "bank", "interest", "tax", "budget", "trade", "tariff", "tariffs"},
     ["stock market screen", "indian rupee notes", "bank building", "financial charts"]),
    ({"company", "startup", "ceo", "acquisition", "merger", "funding", "ipo", "revenue", "profit", "layoffs"},
     ["office meeting", "skyscrapers city", "business handshake", "startup office"]),
    ({"police", "arrest", "arrested", "crime", "investigation", "cbi", "raid", "probe"},
     ["police car lights", "police officers", "investigation documents"]),
    ({"earthquake", "earthquakes", "quake", "tremor", "tremors", "magnitude", "tsunami", "aftershock", "seismic"},
     ["collapsed building rubble", "rescue workers rubble", "emergency rescue team", "ocean waves crashing"]),
    ({"flood", "floods", "cyclone", "rain", "rains", "monsoon", "landslide", "heatwave", "storm"},
     ["flood water street", "heavy rain city", "storm clouds", "rescue team"]),
    ({"hospital", "doctor", "doctors", "vaccine", "disease", "virus", "outbreak", "health", "patients", "medical"},
     ["hospital corridor", "doctor stethoscope", "medical laboratory", "vaccine syringe"]),
    ({"exam", "exams", "students", "school", "university", "neet", "jee", "board", "college", "education"},
     ["students classroom", "exam hall students", "university campus", "school children"]),
    ({"cricket", "wicket", "innings", "ipl", "bcci", "odi", "t20", "batsman", "bowler", "test"},
     ["cricket match stadium", "cricket bat ball", "stadium crowd cheering", "cricket player batting"]),
    ({"football", "fifa", "striker", "uefa", "soccer", "premier", "nations", "midfielder", "goalkeeper"},
     ["football match stadium", "soccer ball field", "stadium crowd cheering", "football players running"]),
    ({"olympic", "olympics", "asian", "games", "medal", "medals", "athletics", "podium", "championship", "sprinter"},
     ["medal ceremony podium", "athletes running track", "stadium crowd cheering", "sports trophy"]),
    ({"tennis", "wimbledon", "slam", "badminton", "racket"},
     ["tennis match court", "tennis ball racket", "stadium crowd cheering"]),
    ({"film", "movie", "movies", "box", "office", "actor", "actress", "bollywood", "hollywood", "trailer", "cinema",
      "premiere", "director", "release"},
     ["cinema theater audience", "movie camera set", "film clapperboard", "red carpet"]),
    ({"album", "song", "songs", "singer", "concert", "band", "music", "tour", "rapper", "musician"},
     ["concert stage lights", "guitar playing", "music recording studio", "crowd concert"]),
    ({"series", "season", "netflix", "episode", "ott", "streaming", "sitcom"},
     ["watching tv living room", "streaming television", "remote control tv"]),
    ({"ai", "openai", "chatbot", "algorithm", "llm", "model", "models", "gemini", "gpt"},
     ["artificial intelligence", "computer code screen", "robot hand", "server room"]),
    ({"chip", "chips", "semiconductor", "processor", "gpu", "laptop", "smartphone", "iphone", "android", "gadget"},
     ["circuit board", "smartphone screen", "laptop typing", "semiconductor chip"]),
    ({"hack", "hacker", "hackers", "cyber", "breach", "security", "ransomware", "malware", "cybersecurity"},
     ["hacker typing dark", "cyber security lock", "computer code screen"]),
    ({"isro", "nasa", "rocket", "launch", "satellite", "moon", "mars", "space", "astronaut"},
     ["rocket launch", "earth from space", "satellite orbit", "night sky stars"]),
    ({"solar", "wind", "energy", "climate", "emissions", "electric", "ev", "battery", "renewable", "power"},
     ["solar panels", "wind turbines", "electric car charging", "power plant"]),
    ({"railway", "train", "metro", "flight", "airport", "airline", "highway", "aviation"},
     ["train station", "airplane takeoff", "airport terminal", "highway traffic"]),
    ({"summit", "diplomat", "diplomats", "treaty", "talks", "embassy", "un", "united", "nations", "ceasefire"},
     ["flags of nations", "diplomats meeting table", "conference hall", "handshake"]),
    ({"army", "soldiers", "military", "troops", "defence", "defense", "missile", "navy"},
     ["soldiers marching", "military parade", "army vehicles"]),
    ({"farmer", "farmers", "crop", "harvest", "msp", "agriculture", "wheat", "paddy"},
     ["farmer field harvest", "wheat field", "tractor farm", "rice paddy"]),
    ({"bird", "birds", "tiger", "wildlife", "conservation", "forest", "bustard", "elephant", "released", "species"},
     ["wildlife forest", "bird flying", "tiger forest", "national park"]),
    ({"bridge", "construction", "infrastructure", "project", "tunnel", "dam"},
     ["construction site crane", "bridge traffic", "city skyline"]),
    ({"protest", "protesters", "protests", "strike", "rally", "demonstration"},
     ["crowd protest", "people marching street", "crowd street"]),
    ({"festival", "diwali", "eid", "temple", "holi", "puja", "navratri"},
     ["festival lights", "temple india", "crowd celebration"]),
    ({"celebration", "celebrate", "celebrates", "wins", "won", "victory", "champions", "title", "trophy"},
     ["team celebration", "crowd cheering", "confetti celebration", "trophy lift"]),
]

CATEGORY_BROLL = {
    "News": ["city street traffic", "crowd walking city", "government building", "newspaper reading"],
    "Technology": ["technology circuit board", "typing on laptop", "data center servers", "futuristic technology"],
    "Sports": ["stadium crowd", "sports training", "athlete running", "sports trophy"],
    "Entertainment": ["cinema screen", "concert stage lights", "film camera", "red carpet"],
}


def topic_queries(text: str, limit_topics: int = 2) -> list[str]:
    """Pick the concrete visual scenes of the topics that best match the story text."""
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    scored = [(len(kws & words), qs) for kws, qs in TOPIC_VISUALS if kws & words]
    scored.sort(key=lambda x: -x[0])
    out: list[str] = []
    for _, qs in scored[:limit_topics]:
        for q in qs:
            if q not in out:
                out.append(q)
    return out


class StockAuthError(RuntimeError):
    """The provider rejected our API key: retrying other queries would only waste time."""


# ------------------------------------------------------------------ shared download / selection logic
class _StockClient:
    name = ""
    key = ""

    def __init__(self, session=None):
        self.http = session or requests
        self.chosen: list[dict] = []   # for logs: which clip was picked for which search term

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _items(self, query: str) -> list[dict]:  # -> [{"id", "duration", "link"}]
        raise NotImplementedError

    def download(self, url: str, dest: Path) -> Path:
        with self.http.get(url, timeout=90, stream=True) as r:
            if r.status_code != 200:
                raise RuntimeError(f"download HTTP {r.status_code}")
            size = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise RuntimeError("clip too large")
                    f.write(chunk)
        return dest

    def fetch_clips(self, queries: list[str], out_dir: Path, wanted: int, min_seconds: float = 4.0,
                    rng: random.Random | None = None) -> list[Path]:
        """Only clips whose tags/title match the search term are used: irrelevant footage is worse than none."""
        rng = rng or random.Random()
        out_dir.mkdir(parents=True, exist_ok=True)
        got: list[Path] = []
        used: set = set()
        for q in queries:
            if len(got) >= wanted:
                break
            terms = query_terms(q)
            try:
                items = [i for i in self._items(q) if i["id"] not in used and (i.get("duration") or 0) >= min_seconds]
            except StockAuthError as e:
                log.warning("%s - skipping this provider for the run", e)
                return got
            except Exception as e:  # noqa: BLE001
                log.warning("%s search '%s' failed: %s", self.name, q, mask_secrets(str(e))[:100])
                continue
            need = max(1, math.ceil(0.6 * len(terms)))   # most of the search words must appear in the clip's tags
            asked = set(re.findall(r"[a-z]+", " ".join(queries).lower()))
            items = [i for i in items if not (set(re.findall(r"[a-z]+", i.get("text", "").lower())) & (OFF_TOPIC_TAGS - asked))]
            scored = [(overlap(terms, i.get("text", "")), i) for i in items]
            scored = [x for x in scored if x[0] >= need]
            if not scored:
                log.info("%s: nothing relevant for '%s' (skipped %d unrelated clips)", self.name, q, len(items))
                continue
            best = max(sc for sc, _ in scored)
            top = [i for sc, i in scored if sc == best]
            rng.shuffle(top)
            for it in top[:3]:
                try:
                    got.append(self.download(it["link"], out_dir / f"{self.name}_{len(got):02d}.mp4"))
                    used.add(it["id"])
                    self.chosen.append({"query": q, "tags": it.get("text", "")[:60]})
                    break
                except Exception as e:  # noqa: BLE001
                    log.warning("Clip download failed: %s", mask_secrets(str(e))[:100])
        return got


# ------------------------------------------------------------------ Pexels
def pick_file(video: dict) -> dict | None:
    """Best Pexels mp4 rendition: portrait, >=540 px wide, closest to 1080 px (never a huge 4K file)."""
    files = [f for f in video.get("video_files", [])
             if f.get("file_type") == "video/mp4" and f.get("link")
             and (f.get("width") or 0) >= 540 and (f.get("height") or 0) >= (f.get("width") or 0)
             and (f.get("width") or 0) <= 1440]
    return min(files, key=lambda f: abs((f.get("width") or 0) - 1080)) if files else None


class PexelsClient(_StockClient):
    name = "pexels"

    def __init__(self, api_key: str | None = None, session=None):
        super().__init__(session)
        self.key = api_key if api_key is not None else config.PEXELS_API_KEY

    def search(self, query: str, per_page: int = 12) -> list[dict]:
        r = self.http.get(PEXELS_API, headers={"Authorization": self.key}, timeout=20,
                          params={"query": query, "orientation": "portrait", "size": "medium", "per_page": per_page})
        if r.status_code in (401, 403):
            raise StockAuthError("Pexels key rejected (HTTP %d): fix or remove PEXELS_API_KEY in .env" % r.status_code)
        if r.status_code != 200:
            raise RuntimeError(f"Pexels HTTP {r.status_code}")
        return r.json().get("videos", [])

    def _items(self, query: str) -> list[dict]:
        out = []
        for v in self.search(query):
            f = pick_file(v)
            if f:
                slug = re.sub(r"\d+/?$", "", (v.get("url") or "").rstrip("/").rsplit("/", 1)[-1]).replace("-", " ")
                out.append({"id": v.get("id"), "duration": v.get("duration"), "link": f["link"], "text": slug})
        return out


# ------------------------------------------------------------------ Pixabay
def pixabay_pick(hit: dict) -> dict | None:
    """Largest rendition up to 1920 px wide and ~35 MB (skips the empty 'large' slot and 4K / huge files)."""
    best = None
    for q in (hit.get("videos") or {}).values():
        if not q or not q.get("url"):
            continue
        w = q.get("width") or 0
        if 960 <= w <= 1920 and (q.get("size") or 0) <= 35_000_000 and (best is None or w > (best.get("width") or 0)):
            best = q
    return best


class PixabayClient(_StockClient):
    name = "pixabay"

    def __init__(self, api_key: str | None = None, session=None, cache_path: Path | None = None):
        super().__init__(session)
        self.key = api_key if api_key is not None else config.PIXABAY_API_KEY
        self.cache_path = cache_path or (config.DATA_DIR / "stock_cache.json")

    def search(self, query: str, per_page: int = 20) -> list[dict]:
        cache = read_json(self.cache_path, {})
        ck = f"pixabay:{query}"
        hit = cache.get(ck)
        if hit and time.time() - hit["ts"] < CACHE_HOURS * 3600:
            return hit["hits"]
        r = self.http.get(PIXABAY_API, timeout=20, params={
            "key": self.key, "q": query, "per_page": per_page, "video_type": "film", "safesearch": "true",
            "min_width": 1280, "order": "popular"})
        if r.status_code in (401, 403) or (r.status_code == 400 and "key" in str(getattr(r, "text", "")).lower()):
            raise StockAuthError("Pixabay key rejected (HTTP %d): fix or remove PIXABAY_API_KEY in .env" % r.status_code)
        if r.status_code != 200:
            raise RuntimeError(f"Pixabay HTTP {r.status_code}")
        hits = r.json().get("hits", [])
        cache[ck] = {"ts": time.time(), "hits": [{"id": h.get("id"), "duration": h.get("duration"),
                                                   "videos": h.get("videos"), "tags": h.get("tags", ""),
                                                   "pageURL": h.get("pageURL", "")} for h in hits]}
        write_json(self.cache_path, prune_dict_by_age(cache, max_entries=100, max_age_hours=CACHE_HOURS))
        return cache[ck]["hits"]

    def _items(self, query: str) -> list[dict]:
        out = []
        for h in self.search(query):
            f = pixabay_pick(h)
            if f:
                slug = (h.get("pageURL") or "").rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
                out.append({"id": h.get("id"), "duration": h.get("duration"), "link": f["url"],
                            "text": f"{h.get('tags', '')} {slug}"})
        return out


# ------------------------------------------------------------------ your own clips
def local_clips(directory: str | Path, wanted: int, category: str | None = None,
                rng: random.Random | None = None, queries: list[str] | None = None) -> list[Path]:
    """Clips from BROLL_DIR. Clips whose FILE or FOLDER NAME matches the story's visual terms come first
    (e.g. 'election_voting_queue.mp4'), then the sub-folder named like the category (News/Technology/Sports/
    Entertainment), then loose files, then any clip in any sub-folder."""
    d = Path(directory) if directory else None
    if not d or not d.is_dir():
        return []
    ext = {".mp4", ".mov", ".m4v"}
    rng = rng or random.Random()

    def clips_in(folder: Path, recursive: bool = False) -> list[Path]:
        it = folder.rglob("*") if recursive else folder.iterdir()
        return sorted(p for p in it if p.is_file() and p.suffix.lower() in ext)

    everything = clips_in(d, recursive=True)
    picked: list[Path] = []
    if queries:
        terms = set().union(*(query_terms(q) for q in queries)) if queries else set()
        scored = [(overlap(terms, " ".join(p.relative_to(d).with_suffix("").parts).replace("_", " ").replace("-", " ")), p)
                  for p in everything]
        matched = [(sc, p) for sc, p in scored if sc >= 1]
        rng.shuffle(matched)
        picked = [p for _, p in sorted(matched, key=lambda x: -x[0])]
    pool: list[Path] = []
    if category:
        for sub in d.iterdir():
            if sub.is_dir() and sub.name.lower() == category.lower():
                pool = clips_in(sub)
    pool = pool or clips_in(d) or everything
    rng.shuffle(pool)
    out = picked + [p for p in pool if p not in picked]
    return out[:wanted]


def configured_providers(session=None) -> list[_StockClient]:
    """API providers to try, in order, honouring STOCK_PROVIDER."""
    mode = config.STOCK_PROVIDER
    if mode in ("none", "local"):
        return []
    clients = [PexelsClient(session=session), PixabayClient(session=session)]
    if mode in ("pexels", "pixabay"):
        clients = [c for c in clients if c.name == mode]
    return [c for c in clients if c.enabled]


def queries_for(category: str, ai_queries: list[str] | None, text: str = "") -> list[str]:
    """Most specific first: concrete scenes for the story's topic, then the AI's suggestions, then category defaults."""
    out: list[str] = []
    for q in topic_queries(text) + (ai_queries or []) + CATEGORY_BROLL.get(category, CATEGORY_BROLL["News"]):
        q = clean_query(q)
        if q and q not in out:
            out.append(q)
    return out[:10]
