"""Story scoring + deficit-based category balancing.

Fixes the old "News dominates, Technology starves" problem: the next story is picked from the
category that is MOST under-represented versus its configured weight in the last 24 h of
actual posts, instead of simply taking the globally highest scores.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from . import config
from .commons import entity_queries
from .dedupe import cluster_articles, story_fingerprint
from .feeds import DEFAULT_SOURCE_QUALITY, SOURCE_QUALITY

WEIGHTS = {"freshness": 0.30, "source": 0.15, "importance": 0.20, "confirmation": 0.20, "suitability": 0.15}
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


CATEGORY_KEYWORDS = {
    "Entertainment": {"album", "band", "singer", "song", "songs", "music", "concert", "movie", "film", "actor", "actress",
                      "trailer", "bollywood", "hollywood", "oscar", "grammy", "celebrity", "episode", "director",
                      "netflix", "sitcom", "rapper", "tour", "premiere", "soundtrack", "boxoffice"},
    "Sports": {"cricket", "wicket", "innings", "tournament", "league", "goal", "fifa", "ipl", "olympic", "olympics",
               "coach", "football", "tennis", "nba", "nfl", "championship", "squad", "batsman", "bowler", "striker"},
    "Technology": {"ai", "chip", "chips", "software", "app", "apps", "startup", "smartphone", "iphone", "android",
                   "openai", "robot", "cybersecurity", "semiconductor", "laptop", "gpu", "cloud", "developer",
                   "developers", "gadget", "algorithm", "chatbot", "processor", "browser"},
}


def refine_category(category: str, title: str, summary: str) -> str:
    """Feeds mislabel stories (e.g. a music story in a tech feed). Only override when the feed's own category
    has NO supporting keywords and another category is clearly indicated (title hits count double)."""
    t_tok, s_tok = set(re.findall(r"[a-z0-9]+", title.lower())), set(re.findall(r"[a-z0-9]+", summary.lower()))
    scores = {c: 2 * len(t_tok & kw) + len(s_tok & kw) for c, kw in CATEGORY_KEYWORDS.items()}
    own = scores.get(category, 0)
    best, best_score = max(scores.items(), key=lambda kv: kv[1])
    if best != category and best_score >= 3 and own == 0:
        return best
    return category


DIGEST_RE = re.compile(
    r"\b(live updates?|live blog|live:|top (?:\d+ )?(?:stories|headlines|news)|headlines? (?:today|of the day)|"
    r"morning (?:digest|brief)|evening (?:digest|brief)|news (?:today|roundup|round-up|wrap|digest|bulletin)|"
    r"newsletter|briefing|in pictures|photos?:|watch:|video:|quiz|horoscope|panchang|"
    r"today'?s (?:top|news|papers?|cinema|entertainment)|weekly (?:roundup|wrap)|week in review|"
    r"\d+ (?:things|best|top|must)|what to watch|podcast)\b", re.I)


def looks_like_digest(title: str, summary: str, n_sources: int) -> bool:
    """Roundups / live blogs / newsletters mix several stories: never a good single Reel."""
    if DIGEST_RE.search(title):
        return True
    return n_sources == 1 and len(entity_queries(summary[:1200], limit=10)) >= 5


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


def build_clusters(articles: list[dict], now: datetime | None = None, trends: list | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    report = trends if hasattr(trends, "providers") else None          # TrendReport (status-aware) or a plain list
    trends = report.trends if report else (trends or [])
    trend_status = report.status if report else ("ok" if trends else "not-checked")
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
        category = refine_category(category, lead["title"], " ".join(a["summary"] for a in group[:3]))
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
            "digest": looks_like_digest(lead["title"], " ".join(a["summary"] for a in group[:2]), len(sources)),
            "trend": None,
            "breaking": False,
            "signals": {"independent_sources": len(sources), "newest_age_hours": round(newest, 2),
                        "articles_last_3h": sum(1 for a in group if _age_hours(a["published"], now) <= 3),
                        "trend_status": trend_status, "trend_query": None, "trend_provider": None,
                        "trend_volume": None, "trend_volume_unit": None},
        })
    if trends:
        from .trends import bonus_for, match_trend
        for c in out:
            t = match_trend(c["lead"]["title"], c["lead"]["title"] + " " + " ".join(a["summary"][:300] for a in c["articles"][:3]),
                            trends)
            if t:
                c["trend"] = t.query
                c["breakdown"]["trend"] = round(bonus_for(t), 1)
                c["score"] = round(c["score"] + bonus_for(t), 1)
                provider = getattr(t, "provider", "google")
                c["signals"].update(trend_query=t.query, trend_provider=provider,
                                    trend_volume=t.traffic or None,       # only a number the provider really reported
                                    trend_volume_unit="pageviews/day" if provider == "wikipedia" else "searches (approx.)")
    for c in out:
        c["breaking"] = (c["score"] >= config.BREAKING_SCORE and len(c["sources"]) >= config.BREAKING_MIN_SOURCES
                         and c["age_hours"] <= 3)
        # Significant = strongly scored, multi-outlet, still fresh. Category balancing never holds these back.
        c["urgent"] = c["breaking"] or (c["score"] >= config.SIGNIFICANT_SCORE and len(c["sources"]) >= 3
                                        and c["age_hours"] <= 12)
        sig = c["signals"]
        # "trending" needs a real signal (a provider match, or several outlets publishing within hours);
        # an important story that nobody is amplifying yet is labelled "important", not "trending".
        c["kind"] = ("trending" if c["trend"] or sig["articles_last_3h"] >= 3
                     else "important" if c["breakdown"]["importance"] >= 70 or c["urgent"] else "routine")
        c["why"] = explain_score(c)
    return sorted(out, key=lambda c: -c["score"])


def explain_score(c: dict) -> str:
    """One line saying why a story ranks where it does (logged for every top candidate)."""
    bd, sig = c["breakdown"], c.get("signals", {})
    parts = [f"score {c['score']:.0f}", f"{sig.get('independent_sources', len(c['sources']))} outlet(s)",
             f"newest {sig.get('newest_age_hours', c['age_hours']):.1f}h old",
             "fresh {:.0f} / source {:.0f} / importance {:.0f} / confirmation {:.0f}".format(
                 bd["freshness"], bd["source"], bd["importance"], bd["confirmation"])]
    if c.get("trend"):
        vol = sig.get("trend_volume")
        parts.append(f"trend '{c['trend']}' via {sig.get('trend_provider')}" +
                     (f" (~{vol:,} {sig.get('trend_volume_unit')})" if vol else " (volume not reported)"))
    else:
        st = sig.get("trend_status", "not-checked")
        parts.append("no matching trend" + (" - trend providers unavailable, so absence is unknown"
                                            if st in ("unavailable", "partial-unavailable") else ""))
    if c.get("urgent"):
        parts.append("urgent: overrides category balancing")
    parts.append(f"kind={c.get('kind', 'routine')}")
    return " | ".join(parts)


def pick_next(clusters: list[dict], recent_categories: list[str], weights: dict[str, float], *,
              min_score: float, exclude: set[str] | None = None, skip_sensitive: bool = True) -> dict | None:
    exclude = exclude or set()
    eligible = [c for c in clusters
                if c["score"] >= min_score and c["fingerprint"] not in exclude
                and not (skip_sensitive and c["sensitive"])
                and not c.get("digest")                                              # roundups are never single stories
                and (len(c["sources"]) >= config.MIN_SOURCES or c.get("trend"))]     # widely covered or Google-trending
    if not eligible:
        return None
    breaking = [c for c in eligible if c["breaking"] or c.get("urgent")]
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


def pick_format(recent_formats: list[str], weights: dict[str, float]) -> str:
    """Choose carousel vs reel by the same deficit logic used for categories (honours CONTENT_FORMATS)."""
    total = sum(weights.values())
    n = len(recent_formats)
    last = recent_formats[-1] if recent_formats else None
    best, best_val = None, -1e9
    for fmt in sorted(weights):
        deficit = weights[fmt] / total - (recent_formats.count(fmt) / n if n else 0.0)
        if fmt == last and len(weights) > 1:
            deficit -= 0.05  # prefer alternating
        if deficit > best_val:
            best, best_val = fmt, deficit
    return best or "carousel"
