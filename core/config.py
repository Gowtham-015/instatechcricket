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

# --- AI ----------------------------------------------------------------
GEMINI_API_KEY = _s("GEMINI_API_KEY")
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
CATEGORY_WEIGHTS = parse_weights(_s("CATEGORY_WEIGHTS", "News:3,Technology:3,Sports:2,Entertainment:2"))

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
    vals = [IG_ACCESS_TOKEN, GEMINI_API_KEY, CLOUDINARY_API_SECRET, CLOUDINARY_API_KEY, GITHUB_TOKEN]
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
    if not POST_TIMES:
        problems.append("POST_TIMES is empty")
    for t in POST_TIMES:
        try:
            h, m = t.split(":")
            assert 0 <= int(h) < 24 and 0 <= int(m) < 60
        except Exception:
            problems.append(f"POST_TIMES entry '{t}' is not HH:MM")
    return problems
