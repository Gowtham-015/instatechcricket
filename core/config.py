"""Central, validated configuration. ONE name per secret (no aliases)."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # dotenv optional in CI
    pass

BASE_DIR = Path(__file__).resolve().parent.parent


def _s(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip().strip('"').strip("'")


def _key(name: str) -> str:
    """API key that ignores obvious placeholders (your_key, xxx, <paste>, changeme) left in .env."""
    v = _s(name)
    low = v.lower()
    if low.startswith(("your", "xxx", "<", "changeme", "paste", "insert")) or "your_" in low or "_here" in low:
        return ""
    return v


def _i(name: str, default: int) -> int:
    try:
        return int(_s(name, str(default)))
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(_s(name, str(default)))
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    return _s(name, str(default)).lower() in {"1", "true", "yes", "on"}


def parse_weights(raw: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in raw.split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                if float(v) > 0:
                    out[k.strip()] = float(v)
            except ValueError:
                continue
    return out


# --- Paths -------------------------------------------------------------
DATA_DIR = Path(_s("DATA_DIR", str(BASE_DIR / "data")))
LOG_DIR = Path(_s("LOG_DIR", str(BASE_DIR / "logs")))
PUBLIC_DIR = BASE_DIR / "public" / "media"
PREVIEW_DIR = BASE_DIR / "preview"

# --- Instagram ---------------------------------------------------------
IG_USER_ID = _s("IG_USER_ID")
IG_ACCESS_TOKEN = _s("IG_ACCESS_TOKEN")
IG_API_HOST = _s("IG_API_HOST", "https://graph.facebook.com").rstrip("/")
IG_API_VERSION = _s("IG_API_VERSION", "v26.0")
IG_DAILY_API_LIMIT = _i("IG_DAILY_API_LIMIT", 50)  # conservative (carousel limit)

# --- Facebook Page (same content as Instagram) ------------------------
FB_PAGE_ID = _s("FB_PAGE_ID")
FB_PAGE_ACCESS_TOKEN = _s("FB_PAGE_ACCESS_TOKEN")      # a PAGE token from Facebook Login (not the Instagram Login token)
FB_API_VERSION = _s("FB_API_VERSION", IG_API_VERSION)
FB_MODE = _s("FB_MODE", "crosspost").lower()           # off | crosspost | link | both

# --- AI ----------------------------------------------------------------
GEMINI_API_KEY = _key("GEMINI_API_KEY")
GEMINI_MODEL = _s("GEMINI_MODEL", "gemini-flash-latest")  # alias -> current Flash; auto-discovery if missing
AI_MAX_ATTEMPTS = _i("AI_MAX_ATTEMPTS", 2)
AI_CACHE_TTL_HOURS = _i("AI_CACHE_TTL_HOURS", 24)

# --- Brand -------------------------------------------------------------
BRAND_NAME = _s("BRAND_NAME", "Daily Pulse")
ACCOUNT_HANDLE = _s("ACCOUNT_HANDLE", "@yourhandle")
BRAND_HASHTAGS = [h for h in _s("BRAND_HASHTAGS", "").split() if h.startswith("#")]
FONT_PATH = _s("FONT_PATH")  # optional custom .ttf (bold)

# --- Media hosting -----------------------------------------------------
MEDIA_HOST = _s("MEDIA_HOST", "cloudinary").lower()
CLOUDINARY_CLOUD_NAME = _s("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = _s("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = _s("CLOUDINARY_API_SECRET")
GITHUB_TOKEN = _s("GITHUB_TOKEN")
GITHUB_REPOSITORY = _s("GITHUB_REPOSITORY")
MEDIA_BRANCH = _s("MEDIA_BRANCH", "media")
MEDIA_BASE_URL = _s("MEDIA_BASE_URL").rstrip("/")

# --- Posting plan ------------------------------------------------------
TIMEZONE = _s("TIMEZONE", "Asia/Kolkata")
POST_TIMES = [t.strip() for t in _s("POST_TIMES", "09:00,13:00,18:00,21:00").split(",") if t.strip()]
SLOT_GRACE_MINUTES = _i("SLOT_GRACE_MINUTES", 90)
MAX_POSTS_PER_DAY = _i("MAX_POSTS_PER_DAY", 6)
MIN_POST_INTERVAL_MINUTES = _i("MIN_POST_INTERVAL_MINUTES", 60)
CATEGORY_WEIGHTS = parse_weights(_s("CATEGORY_WEIGHTS", "News:3,Technology:3,Sports:2,Entertainment:2,Business:2,Science:1"))

# --- Formats (carousel = image slides, reel = vertical video) -----------
CONTENT_FORMATS = parse_weights(_s("CONTENT_FORMATS", "carousel:1,reel:1"))
VIDEO_SECONDS_PER_SLIDE = _f("VIDEO_SECONDS_PER_SLIDE", 4.0)
VIDEO_FPS = _i("VIDEO_FPS", 30)
MUSIC_PATH = _s("MUSIC_PATH")  # optional ROYALTY-FREE audio file
REEL_SHARE_TO_FEED = _b("REEL_SHARE_TO_FEED", True)
REEL_STYLE = _s("REEL_STYLE", "dynamic").lower()          # dynamic = footage + voiceover + captions, cards = old slideshow
STOCK_PROVIDER = _s("STOCK_PROVIDER", "auto").lower()      # auto | pexels | pixabay | local | none
PEXELS_API_KEY = _key("PEXELS_API_KEY")                      # https://www.pexels.com/api/ (new keys may be paused)
PIXABAY_API_KEY = _key("PIXABAY_API_KEY")                    # free: https://pixabay.com/api/docs/
BROLL_DIR = _s("BROLL_DIR")                                # optional folder of your OWN vertical .mp4 clips
TTS_PROVIDER = _s("TTS_PROVIDER", "auto").lower()          # auto | edge | gemini | none
TTS_VOICE = _s("TTS_VOICE", "en-IN-NeerjaNeural")
# Voices rotated between Reels (a different voice than the previous Reel). Set TTS_VOICES to one voice to pin it.
# If you only set TTS_VOICE, that single voice is used for every Reel.
TTS_VOICES = [v.strip() for v in _s("TTS_VOICES", "" if os.getenv("TTS_VOICE") else "en-IN-NeerjaNeural,en-IN-PrabhatNeural").split(",") if v.strip()]
EXTRA_FEEDS = _s("EXTRA_FEEDS", "")   # more RSS sources: "News|The Print|https://theprint.in/feed/;Sports|Name|https://..."
REEL_VARIETY = _b("REEL_VARIETY", True)   # change voice, caption colour, headline layout, grade, outro and script opening per Reel
TTS_RATE = _s("TTS_RATE", "+6%")
GEMINI_TTS_VOICE = _s("GEMINI_TTS_VOICE", "Kore")        # used when the Gemini voice is the one speaking
MUSIC_VOLUME = _f("MUSIC_VOLUME", 0.12)                    # background music level under the voice (0..1)
REAL_PHOTOS = _b("REAL_PHOTOS", True)                      # real documentary photos from Wikimedia Commons (free licences)
FOOTAGE_CREDIT = _b("FOOTAGE_CREDIT", True)                # add "Footage: Pexels" to captions when stock is used
IG_VIDEO_POLL_TIMEOUT = _i("IG_VIDEO_POLL_TIMEOUT", 600)

# --- Trending ----------------------------------------------------------
TRENDS_ENABLED = _b("TRENDS_ENABLED", True)    # boost stories matching Google Trends' "trending now" searches
TRENDS_GEO = _s("TRENDS_GEO", "IN")
MIN_SOURCES = _i("MIN_SOURCES", 2)             # a story must be reported by >=N outlets (or match a Google trend)

# --- Photos ------------------------------------------------------------
PHOTO_DIR = _s("PHOTO_DIR")                              # YOUR OWN photos (licensed/permitted): see README
PHOTO_SOURCES = [x.strip() for x in _s("PHOTO_SOURCES", "commons,openverse").lower().split(",") if x.strip()]
OPENVERSE_CLIENT_ID = _s("OPENVERSE_CLIENT_ID")        # optional, free: raises the anonymous limit (5/hour, 100/day)
OPENVERSE_CLIENT_SECRET = _s("OPENVERSE_CLIENT_SECRET")
OPENVERSE_SOURCES = _s("OPENVERSE_SOURCES", "flickr,nasa,smithsonian")

# --- Editorial engine (format decision + authentic media) -------------
FORMAT_POLICY = _s("FORMAT_POLICY", "editorial").lower()   # editorial = story + media decide | legacy = CONTENT_FORMATS weights
MEDIA_SOURCE_ORDER = [x.strip() for x in _s("MEDIA_SOURCE_ORDER", "official,local,commons,openverse").lower().split(",") if x.strip()]
MEDIA_MIN_CONFIDENCE = _f("MEDIA_MIN_CONFIDENCE", 0.6)     # 0..1: a photo/video below this is rejected
MEDIA_MIN_WIDTH = _i("MEDIA_MIN_WIDTH", 1000)              # px, shortest acceptable photo width
MEDIA_MIN_HEIGHT = _i("MEDIA_MIN_HEIGHT", 600)
MEDIA_LICENSE_POLICY = _s("MEDIA_LICENSE_POLICY", "strict").lower()  # strict = CC0/PD/CC BY/GODL/official allow-list/own files only
OFFICIAL_MEDIA_DOMAINS = [x.strip().lower() for x in _s("OFFICIAL_MEDIA_DOMAINS", "nasa.gov,images.nasa.gov").split(",") if x.strip()]
VIDEO_DIR = _s("VIDEO_DIR")                                # your authorised event videos (+ a .rights.json file each)
COMMONS_VIDEO = _b("COMMONS_VIDEO", True)                  # openly licensed videos from Wikimedia Commons
ALLOW_FILE_PHOTOS = _b("ALLOW_FILE_PHOTOS", True)          # a photo of the right person/place, not of the event, labelled "File photo"
ILLUSTRATIVE_CATEGORIES = [x.strip() for x in _s("ILLUSTRATIVE_CATEGORIES", "Technology,Science,Business").split(",") if x.strip()]
REEL_VOICEOVER = _s("REEL_VOICEOVER", "auto").lower()      # auto | always | never
REEL_NARRATION_MIN = _f("REEL_NARRATION_MIN", 0.6)         # how much a story must gain from narration before it becomes a Reel
EDITORIAL_MIN_CONFIDENCE = _f("EDITORIAL_MIN_CONFIDENCE", 0.35)  # story confidence below this is held back
MIN_CORROBORATION_SENSITIVE = _i("MIN_CORROBORATION_SENSITIVE", 2)  # independent outlets for election/health/disaster/conflict
SOURCES_FILE = _s("SOURCES_FILE")                          # JSON file: add/remove/disable news feeds without code
DISABLED_SOURCES = [x.strip() for x in _s("DISABLED_SOURCES", "").split(",") if x.strip()]
TREND_PROVIDERS = [x.strip().lower() for x in _s("TREND_PROVIDERS", "google,wikipedia").split(",") if x.strip()]
SIGNIFICANT_SCORE = _f("SIGNIFICANT_SCORE", 75.0)           # a story this strong (and widely reported) overrides category balancing
CAROUSEL_MIN_PHOTOS = _i("CAROUSEL_MIN_PHOTOS", 2)          # distinct accepted photos needed before a photo carousel is considered
VIDEO_MIN_SECONDS = _f("VIDEO_MIN_SECONDS", 3.0)            # an authorised video must be at least this long ...
VIDEO_MAX_SECONDS = _f("VIDEO_MAX_SECONDS", 90.0)           # ... and is trimmed to this (Instagram allows up to 15 min; 90 s keeps news Reels tight)
EVENT_WINDOW_DAYS = _i("EVENT_WINDOW_DAYS", 3)              # media older than this can never be labelled as the event itself
DECISION_LOG_MAX = _i("DECISION_LOG_MAX", 100)              # editorial decision records kept in data/decisions.jsonl

# --- Collection / ranking ---------------------------------------------
MAX_NEWS_AGE_HOURS = _i("MAX_NEWS_AGE_HOURS", 24)
MIN_STORY_SCORE = _f("MIN_STORY_SCORE", 35.0)
BREAKING_SCORE = _f("BREAKING_SCORE", 85.0)
BREAKING_MIN_SOURCES = _i("BREAKING_MIN_SOURCES", 3)
SKIP_SENSITIVE = _b("SKIP_SENSITIVE", True)
MAX_PUBLISH_ATTEMPTS_PER_RUN = _i("MAX_PUBLISH_ATTEMPTS_PER_RUN", 2)
BREAKING_ENABLED = _b("BREAKING_ENABLED", True)
ALLOW_FALLBACK_POSTS = _b("ALLOW_FALLBACK_POSTS", True)

# --- Retention (keeps committed state tiny) ----------------------------
HISTORY_MAX_RECORDS = _i("HISTORY_MAX_RECORDS", 400)
HISTORY_MAX_DAYS = _i("HISTORY_MAX_DAYS", 60)
AI_CACHE_MAX_ENTRIES = _i("AI_CACHE_MAX_ENTRIES", 150)

# --- Ops ---------------------------------------------------------------
DRY_RUN = _b("DRY_RUN", False)
ALERT_WEBHOOK_URL = _s("ALERT_WEBHOOK_URL")
ALERT_THROTTLE_HOURS = _i("ALERT_THROTTLE_HOURS", 4)
BREAKER_FAILURES = _i("BREAKER_FAILURES", 5)
BREAKER_PAUSE_HOURS = _i("BREAKER_PAUSE_HOURS", 3)


def secrets_to_mask() -> list[str]:
    vals = [IG_ACCESS_TOKEN, GEMINI_API_KEY, CLOUDINARY_API_SECRET, CLOUDINARY_API_KEY, GITHUB_TOKEN, PEXELS_API_KEY, PIXABAY_API_KEY, OPENVERSE_CLIENT_SECRET,
            FB_PAGE_ACCESS_TOKEN]
    return [v for v in vals if v and len(v) >= 6]


def validate(mode: str = "publish") -> list[str]:
    """Return a list of human-readable problems (empty list == OK)."""
    problems: list[str] = []
    if mode == "publish":
        if not IG_USER_ID:
            problems.append("IG_USER_ID is missing")
        if not IG_ACCESS_TOKEN:
            problems.append("IG_ACCESS_TOKEN is missing")
        if MEDIA_HOST == "cloudinary":
            for n, v in (("CLOUDINARY_CLOUD_NAME", CLOUDINARY_CLOUD_NAME),
                         ("CLOUDINARY_API_KEY", CLOUDINARY_API_KEY),
                         ("CLOUDINARY_API_SECRET", CLOUDINARY_API_SECRET)):
                if not v:
                    problems.append(f"{n} is missing (MEDIA_HOST=cloudinary)")
        elif MEDIA_HOST == "github":
            if not GITHUB_TOKEN:
                problems.append("GITHUB_TOKEN is missing (MEDIA_HOST=github)")
            if "/" not in GITHUB_REPOSITORY:
                problems.append("GITHUB_REPOSITORY must look like 'owner/repo'")
        elif MEDIA_HOST == "local":
            if not MEDIA_BASE_URL.startswith("https://"):
                problems.append("MEDIA_BASE_URL must be a public https:// URL (MEDIA_HOST=local)")
        else:
            problems.append(f"MEDIA_HOST '{MEDIA_HOST}' is invalid (cloudinary|github|local)")
    if not CATEGORY_WEIGHTS:
        problems.append("CATEGORY_WEIGHTS is empty/invalid")
    if not CONTENT_FORMATS or set(CONTENT_FORMATS) - {"carousel", "reel", "photo"}:
        problems.append("CONTENT_FORMATS must use only carousel, reel and/or photo, e.g. carousel:1,reel:1")
    if FORMAT_POLICY not in {"editorial", "legacy"}:
        problems.append("FORMAT_POLICY must be editorial or legacy")
    if MEDIA_LICENSE_POLICY not in {"strict"}:
        problems.append("MEDIA_LICENSE_POLICY must be strict")
    if not 23 <= VIDEO_FPS <= 60:
        problems.append("VIDEO_FPS must be 23-60 (Instagram Reels range)")
    if not 0.0 <= MEDIA_MIN_CONFIDENCE <= 1.0:
        problems.append("MEDIA_MIN_CONFIDENCE must be between 0 and 1")
    if set(MEDIA_SOURCE_ORDER) - {"official", "local", "commons", "openverse"}:
        problems.append("MEDIA_SOURCE_ORDER may only contain official, local, commons, openverse")
    if set(TREND_PROVIDERS) - {"google", "wikipedia"}:
        problems.append("TREND_PROVIDERS may only contain google, wikipedia")
    if REEL_VOICEOVER not in {"auto", "always", "never"}:
        problems.append("REEL_VOICEOVER must be auto, always or never")
    if REEL_STYLE not in {"dynamic", "cards"}:
        problems.append("REEL_STYLE must be dynamic or cards")
    if STOCK_PROVIDER not in {"auto", "pexels", "pixabay", "local", "none"}:
        problems.append("STOCK_PROVIDER must be auto, pexels, pixabay, local or none")
    if FB_MODE not in {"off", "crosspost", "link", "both"}:
        problems.append("FB_MODE must be off, crosspost, link or both")
    if bool(FB_PAGE_ID) != bool(FB_PAGE_ACCESS_TOKEN):
        problems.append("Facebook needs BOTH FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN (or neither)")
    if set(PHOTO_SOURCES) - {"commons", "openverse"}:
        problems.append("PHOTO_SOURCES may only contain commons and/or openverse")
    if TTS_PROVIDER not in {"auto", "edge", "gemini", "none"}:
        problems.append("TTS_PROVIDER must be auto, edge, gemini or none")
    if not POST_TIMES:
        problems.append("POST_TIMES is empty")
    for t in POST_TIMES:
        try:
            h, m = t.split(":")
            assert 0 <= int(h) < 24 and 0 <= int(m) < 60
        except Exception:
            problems.append(f"POST_TIMES entry '{t}' is not HH:MM")
    return problems
