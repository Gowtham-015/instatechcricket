"""'Trending now' signal from Google Trends' public RSS feed (https://trends.google.com/trending/rss?geo=IN).

Google publishes ~10 trending searches per country with a traffic estimate ("200+", "100K+", "2M+") and the news
stories behind each trend. A story that matches a trend gets a ranking bonus. Any failure -> no bonus, never an error.
"""
from __future__ import annotations

import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import requests

from . import config
from .dedupe import jaccard, tokens
from .storage import read_json, write_json
from .stock import overlap, query_terms

log = logging.getLogger("trends")
URL = "https://trends.google.com/trending/rss?geo={geo}"
NS = {"ht": "https://trends.google.com/trending/rss"}
CACHE_MINUTES = 60


@dataclass
class Trend:
    query: str
    traffic: int
    news_titles: list[str] = field(default_factory=list)
    provider: str = "google"


def parse_traffic(text: str | None) -> int:
    m = re.match(r"\s*([\d.,]+)\s*([KkMm]?)", text or "")
    if not m:
        return 0
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    return int(n * {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()])


def parse_feed(xml_text: str) -> list[Trend]:
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        news = [(n.findtext("ht:news_item_title", namespaces=NS) or "").strip()
                for n in item.findall("ht:news_item", NS)]
        out.append(Trend(title, parse_traffic(item.findtext("ht:approx_traffic", namespaces=NS)),
                         [n for n in news if n]))
    return out


@dataclass
class TrendReport:
    """What the trend providers actually delivered. `unavailable` (a provider could not be reached) is NOT the same as
    `empty` (the provider answered and nothing is trending): ranking logs and decision records keep them apart."""
    providers: dict = field(default_factory=dict)     # name -> {"status": ok|empty|unavailable|disabled, "count": n, "detail": str}
    trends: list = field(default_factory=list)

    @property
    def status(self) -> str:
        states = {v["status"] for v in self.providers.values()}
        if "ok" in states:
            return "ok"
        if states == {"unavailable"}:
            return "unavailable"
        if "unavailable" in states:
            return "partial-unavailable"
        return "empty" if "empty" in states else "disabled"

    def describe(self) -> str:
        return "; ".join(f"{n}: {v['status']}" + (f" ({v['detail']})" if v.get("detail") else "") +
                         (f" [{v['count']} trends]" if v.get("count") else "")
                         for n, v in self.providers.items()) or "no trend providers configured"


WIKI_TOP = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{y}/{m:02d}/{d:02d}"
_WIKI_SKIP = ("Main_Page", "Special:", "Wikipedia:", "Portal:", "File:", "Help:", "Category:", "Talk:", "-")


def parse_wikipedia_top(payload: dict, limit: int = 40) -> list[Trend]:
    """Most-viewed English Wikipedia articles of a day. The view counts are Wikimedia's real numbers."""
    items = (((payload or {}).get("items") or [{}])[0]).get("articles") or []
    out = []
    for a in items:
        title = str(a.get("article", ""))
        if not title or title.startswith(_WIKI_SKIP) or title == "-":
            continue
        views = int(a.get("views") or 0)
        out.append(Trend(title.replace("_", " "), views))
        if len(out) >= limit:
            break
    return out


def fetch_wikipedia_trends(session=None, now=None) -> list[Trend]:
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc)
    day = now - timedelta(days=1)          # yesterday is the latest complete day
    r = (session or requests).get(WIKI_TOP.format(y=day.year, m=day.month, d=day.day), timeout=20,
                                  headers={"User-Agent": "InstaNewsAgent/1.0 (news ranking; contact via account bio)"})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return parse_wikipedia_top(r.json())


def fetch_trend_report(session=None, providers: list[str] | None = None, cache_path: Path | None = None) -> TrendReport:
    rep = TrendReport()
    for name in (providers if providers is not None else config.TREND_PROVIDERS):
        try:
            if name == "google":
                got = fetch_trends(session=session, cache_path=cache_path, raise_errors=True)
            elif name == "wikipedia":
                got = fetch_wikipedia_trends(session=session)
            else:
                rep.providers[name] = {"status": "disabled", "count": 0, "detail": "unknown provider"}
                continue
        except Exception as e:  # noqa: BLE001 - a provider outage must never stop the pipeline
            rep.providers[name] = {"status": "unavailable", "count": 0, "detail": f"{type(e).__name__}: {str(e)[:80]}"}
            log.info("Trend provider %s unavailable (%s) - ranking on the other signals", name, type(e).__name__)
            continue
        rep.providers[name] = {"status": "ok" if got else "empty", "count": len(got), "detail": ""}
        for t in got:
            t.provider = name
        rep.trends += got
    return rep


def fetch_trends(geo: str | None = None, session=None, cache_path: Path | None = None,
                 raise_errors: bool = False) -> list[Trend]:
    geo = (geo or config.TRENDS_GEO).upper()
    cache_path = cache_path or (config.DATA_DIR / "trends_cache.json")
    cache = read_json(cache_path, {})
    if cache.get("geo") == geo and time.time() - cache.get("ts", 0) < CACHE_MINUTES * 60 and cache.get("xml"):
        try:
            return parse_feed(cache["xml"])
        except ET.ParseError:
            pass
    try:
        r = (session or requests).get(URL.format(geo=geo), timeout=20,
                                      headers={"User-Agent": "Mozilla/5.0 (compatible; InstaNewsAgent/1.0)"})
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        trends = parse_feed(r.text)
    except Exception as e:  # noqa: BLE001 - trending is a bonus signal, never a requirement
        log.info("Google Trends unavailable (%s) - ranking without it", type(e).__name__)
        if raise_errors:
            raise
        return []
    write_json(cache_path, {"geo": geo, "ts": time.time(), "xml": r.text})
    return trends


def bonus_for(trend: Trend) -> float:
    if trend.provider == "wikipedia":      # reference-site pageviews are a weaker "trending now" signal than search trends
        return min(10.0, 3.0 + 2.0 * math.log10(max(trend.traffic / 10000.0, 1.0)))
    return min(25.0, 12.0 + 4.0 * math.log10(max(trend.traffic / 1000.0, 1.0)))


def match_trend(title: str, text: str, trends: list[Trend]) -> Trend | None:
    """Best-matching trend for a story: query words found in the story, or the story matches one of the news
    headlines Google links to that trend."""
    title_tokens = tokens(title)
    best = None
    for t in trends:
        hit = False
        terms = query_terms(t.query)
        if terms:
            need = max(1, math.ceil(0.6 * len(terms)))
            if overlap(terms, text) >= need and (len(terms) >= 2 or overlap(terms, title) >= 1):
                hit = True
        if not hit:
            for nt in t.news_titles:
                nt_tokens = tokens(nt)
                if len(title_tokens & nt_tokens) >= 3 and jaccard(title_tokens, nt_tokens) >= 0.35:
                    hit = True
                    break
        if hit and (best is None or t.traffic > best.traffic):
            best = t
    return best
