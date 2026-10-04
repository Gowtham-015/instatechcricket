"""Story scoring + deficit-based category balancing.

Fixes the old "News dominates, Technology starves" problem: the next story is picked from the
category that is MOST under-represented versus its configured weight in the last 24 h of
actual posts, instead of simply taking the globally highest scores.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from . import config
from .dedupe import cluster_articles, story_fingerprint
from .feeds import DEFAULT_SOURCE_QUALITY, SOURCE_QUALITY

WEIGHTS = {"freshness": 0.30, "source": 0.20, "importance": 0.20, "confirmation": 0.15, "suitability": 0.15}
HALF_LIFE_HOURS = 6.0

HIGH_SIGNAL = (
    "announces", "announced", "launch", "launches", "unveils", "wins", "won", "record", "final", "semi-final",
    "election", "budget", "verdict", "ruling", "ban", "resigns", "appointed", "acquires", "acquisition",
    "breakthrough", "partnership", "deal", "champion", "trophy", "release", "released", "debut", "oscar",
    "ipo", "merger", "policy", "ai", "chip", "tournament", "box office", "series", "squad",
)
SENSITIVE = (
    "suicide", "rape", "sexual assault", "molest", "murder", "beheaded", "gore", "death toll", "dead body",
    "child abuse", "mass shooting", "stabbed", "lynching", "self-harm", "acid attack",
)


def is_sensitive(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in SENSITIVE)


def freshness_score(age_hours: float) -> float:
    return 100.0 * math.pow(0.5, max(age_hours, 0.0) / HALF_LIFE_HOURS)


def importance_score(text: str) -> float:
    low = f" {text.lower()} "
    hits = sum(1 for k in HIGH_SIGNAL if f" {k} " in low or f" {k}," in low)
    return min(100.0, 40.0 + 15.0 * hits)


def suitability_score(lead: dict) -> float:
    s = 60.0 * min(1.0, len(lead.get("summary", "")) / 160.0)
    tl = len(lead["title"])
    s += 40.0 if 30 <= tl <= 110 else 20.0
    return s


def _age_hours(iso: str | None, now: datetime) -> float:
    if not iso:
        return 99.0
    return (now - datetime.fromisoformat(iso)).total_seconds() / 3600.0


def build_clusters(articles: list[dict], now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    out = []
    for group in cluster_articles(articles):
        group.sort(key=lambda a: (-SOURCE_QUALITY.get(a["source"], DEFAULT_SOURCE_QUALITY),
                                  _age_hours(a["published"], now)))
        lead = group[0]
        sources = sorted({a["source"] for a in group})
        cats: dict[str, int] = {}
        for a in group:
            cats[a["category"]] = cats.get(a["category"], 0) + 1
        category = sorted(cats, key=lambda c: (-cats[c], c))[0]
        newest = min(_age_hours(a["published"], now) for a in group)
        text = " ".join(a["title"] + " " + a["summary"] for a in group)
        bd = {
            "freshness": freshness_score(newest),
            "source": 100.0 * max(SOURCE_QUALITY.get(s, DEFAULT_SOURCE_QUALITY) for s in sources),
            "importance": importance_score(lead["title"] + " " + lead["summary"]),
            "confirmation": 100.0 * min(1.0, (len(sources) - 1) / 3.0),
            "suitability": suitability_score(lead),
        }
        score = sum(bd[k] * w for k, w in WEIGHTS.items())
        out.append({
            "fingerprint": story_fingerprint([a["title"] for a in group]),
            "category": category,
            "articles": group,
            "lead": lead,
            "sources": sources,
            "age_hours": round(newest, 2),
            "score": round(score, 1),
            "breakdown": {k: round(v, 1) for k, v in bd.items()},
            "sensitive": is_sensitive(text),
            "breaking": False,
        })
    for c in out:
        c["breaking"] = (c["score"] >= config.BREAKING_SCORE and len(c["sources"]) >= config.BREAKING_MIN_SOURCES
                         and c["age_hours"] <= 3)
    return sorted(out, key=lambda c: -c["score"])


def pick_next(clusters: list[dict], recent_categories: list[str], weights: dict[str, float], *,
              min_score: float, exclude: set[str] | None = None, skip_sensitive: bool = True) -> dict | None:
    exclude = exclude or set()
    eligible = [c for c in clusters
                if c["score"] >= min_score and c["fingerprint"] not in exclude
                and not (skip_sensitive and c["sensitive"])]
    if not eligible:
        return None
    breaking = [c for c in eligible if c["breaking"]]
    if breaking:
        return max(breaking, key=lambda c: c["score"])

    total_w = sum(weights.values())
    n = len(recent_categories)
    last = recent_categories[-1] if recent_categories else None
    best_cat, best_val = None, -1e9
    for cat, w in weights.items():
        cands = [c for c in eligible if c["category"] == cat]
        if not cands:
            continue
        deficit = w / total_w - (recent_categories.count(cat) / n if n else 0.0)
        if cat == last:
            deficit -= 0.15  # gentle anti-streak
        val = deficit + max(c["score"] for c in cands) / 10000.0  # score only breaks ties
        if val > best_val:
            best_cat, best_val = cat, val
    if best_cat is None:  # categories outside the weight table
        return max(eligible, key=lambda c: c["score"])
    return max((c for c in eligible if c["category"] == best_cat), key=lambda c: c["score"])


def ordered_candidates(clusters: list[dict], recent_categories: list[str], weights: dict[str, float], *,
                       min_score: float, exclude: set[str] | None = None, limit: int = 3,
                       skip_sensitive: bool = True) -> list[dict]:
    """Best-first candidate list (used for fall-back if the first story fails)."""
    exclude = set(exclude or ())
    recent = list(recent_categories)
    picks: list[dict] = []
    for _ in range(limit):
        c = pick_next(clusters, recent, weights, min_score=min_score, exclude=exclude,
                      skip_sensitive=skip_sensitive)
        if not c:
            break
        picks.append(c)
        exclude.add(c["fingerprint"])
        recent.append(c["category"])
    return picks
