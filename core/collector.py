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
from .feeds import FEEDS
from .storage import retry

log = logging.getLogger("collector")


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
    articles: list[dict] = []
    failures = 0
    total = sum(len(v) for v in (feeds or FEEDS).values())
    for category, flist in (feeds or FEEDS).items():
        for feed in flist:
            try:
                items = fetch_feed(feed, category, session=session)
            except Exception as e:  # one broken feed must never stop the run
                failures += 1
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
    log.info("Collected %d fresh articles (%d feed failures)", len(articles), failures)
    return articles
