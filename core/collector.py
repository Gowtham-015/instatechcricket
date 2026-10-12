"""Fetch RSS feeds safely (timeouts, retries, per-feed isolation) and normalise articles."""
from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from time import mktime

import feedparser
import requests

from . import config
from .feeds import all_feeds
from .storage import retry

log = logging.getLogger("collector")
LAST_REPORT: dict = {"feeds_total": 0, "feeds_ok": 0, "feeds_failed": [], "articles": 0, "by_category": {}}


class CollectionError(RuntimeError):
    """Every feed failed -> network/DNS outage, must be reported instead of silently idling."""
UA = {"User-Agent": "Mozilla/5.0 (compatible; InstaNewsAgent/1.0)"}
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
SPAM_WORDS = ("giveaway", "coupon", "promo code", "sponsored", "advertorial", "horoscope", "live updates:")


def clean_text(raw: str) -> str:
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", raw or ""))).strip()


def article_id(url: str, title: str) -> str:
    return hashlib.sha1(f"{url}|{title}".encode("utf-8")).hexdigest()[:16]


def parse_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime.fromtimestamp(mktime(st), tz=timezone.utc)
            except (OverflowError, ValueError):
                continue
    return None


def is_quality(article: dict) -> bool:
    title = article["title"]
    if len(title) < 20 or len(title.split()) < 4:
        return False
    low = (title + " " + article["summary"]).lower()
    return not any(w in low for w in SPAM_WORDS)


_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")
_VID_EXT = (".mp4", ".mov", ".webm", ".m4v")


def extract_media(entry) -> list[dict]:
    """Image/video links a feed item itself supplies (media:content, media:thumbnail, enclosures). They are only
    METADATA here: nothing is downloaded unless the media layer can document the right to reuse it."""
    out: list[dict] = []
    seen: set[str] = set()
    lic = ""
    raw = entry.get("license") or entry.get("rights") or ""
    if isinstance(raw, str):
        lic = raw.strip()

    def add(url, mtype="", medium="", width=0, height=0):
        url = (url or "").strip()
        if not url.startswith("http") or url in seen:
            return
        low = url.split("?")[0].lower()
        kind = "video" if (medium == "video" or mtype.startswith("video") or low.endswith(_VID_EXT)) else \
            "photo" if (medium == "image" or mtype.startswith("image") or low.endswith(_IMG_EXT)) else ""
        if not kind:
            return
        seen.add(url)
        credit = entry.get("media_credit")
        if isinstance(credit, list) and credit:
            credit = credit[0].get("content") if isinstance(credit[0], dict) else credit[0]
        out.append({"kind": kind, "url": url, "width": int(width or 0), "height": int(height or 0),
                    "credit": str(credit or "")[:80], "licence_hint": lic})

    for m in entry.get("media_content", []) or []:
        add(m.get("url"), m.get("type", ""), m.get("medium", ""), m.get("width", 0), m.get("height", 0))
    for m in entry.get("media_thumbnail", []) or []:
        add(m.get("url"), "image", "image", m.get("width", 0), m.get("height", 0))
    for enc in entry.get("enclosures", []) or []:
        add(enc.get("href") or enc.get("url"), enc.get("type", ""))
    return out[:6]


def parse_entry(entry, source: str, category: str) -> dict | None:
    title = clean_text(entry.get("title", ""))
    url = (entry.get("link") or "").strip()
    if not title or not url.startswith("http"):
        return None
    summary = clean_text(entry.get("summary") or entry.get("description") or "")
    if summary.lower().startswith(title.lower()[:40]):
        summary = summary[len(title):].strip(" -–:") or summary
    published = parse_time(entry)
    return {
        "id": article_id(url, title),
        "title": title,
        "summary": summary[:1200],
        "url": url,
        "source": source,
        "category": category,
        "published": published.isoformat() if published else None,
        "media": extract_media(entry),
    }


def fetch_feed(feed: dict, category: str, *, session=None, timeout: int = 15) -> list[dict]:
    http = session or requests
    def _get():
        r = http.get(feed["url"], headers=UA, timeout=timeout)
        r.raise_for_status()
        return r.content
    content = retry(_get, attempts=3, base_delay=1.0, retry_on=(requests.RequestException,))
    parsed = feedparser.parse(content)
    out = []
    for e in parsed.entries:
        a = parse_entry(e, feed["name"], category)
        if a:
            out.append(a)
    return out


def collect_news(*, max_age_hours: int | None = None, feeds: dict | None = None,
                 session=None, now: datetime | None = None) -> list[dict]:
    max_age = max_age_hours or config.MAX_NEWS_AGE_HOURS
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age)
    feeds = feeds or all_feeds()
    articles: list[dict] = []
    failures = 0
    failed_names: list[str] = []
    total = sum(len(v) for v in feeds.values())
    for category, flist in feeds.items():
        for feed in flist:
            try:
                items = fetch_feed(feed, category, session=session)
            except Exception as e:  # one broken feed must never stop the run
                failures += 1
                failed_names.append(feed["name"])
                log.warning("Feed failed: %s (%s)", feed["name"], type(e).__name__)
                continue
            for a in items:
                ts = a["published"]
                if ts is None:
                    continue  # undated items can't be judged fresh -> skip
                if datetime.fromisoformat(ts) < cutoff:
                    continue
                if is_quality(a):
                    articles.append(a)
    if total and failures == total:
        raise CollectionError(f"all {total} feeds failed (network outage?)")
    by_cat: dict[str, int] = {}
    for a in articles:
        by_cat[a["category"]] = by_cat.get(a["category"], 0) + 1
    LAST_REPORT.update(feeds_total=total, feeds_ok=total - failures, feeds_failed=failed_names,
                       articles=len(articles), by_category=by_cat)
    log.info("Collected %d fresh articles from %d/%d feeds (failed: %s) by category %s", len(articles),
             total - failures, total, ", ".join(failed_names) or "none", by_cat)
    return articles
