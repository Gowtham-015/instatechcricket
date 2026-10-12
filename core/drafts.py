"""Remix helper: the agent does the research and writing, YOU do the one-minute in-app Remix/Collab.

Instagram's Remix lets you build a new Reel on top of an official one (BCCI, ICC, a franchise, a news outlet) with
automatic credit to the original, and only works where the original creator allows it. It is the platform-sanctioned
way to use official footage and it cannot be automated through the API, so this command prepares everything else:
the best trending story, a spoken script, an on-screen headline, a ready-to-paste caption with hashtags and sources,
and a step-by-step checklist.
"""
from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from . import config
from .ai_writer import compose_caption
from .dedupe import dedupe_batch, filter_published
from .ranker import build_clusters, ordered_candidates
from .stock import TOPIC_VISUALS
from .storage import parse_iso

CHECKLIST = """HOW TO POST (about 1 minute)
 1. On Instagram, search for the OFFICIAL account/Reel for this story (e.g. the board, the tournament, the team or the
    news outlet that published the clip). Check the account is the verified/official one.
 2. Open the Reel > tap the paper-plane/"..." menu > Remix (not available if the owner switched it off).
    No Remix button? Ask for a Collab or credit-and-permission by DM, or use a different clip.
 3. Record/trim your Remix: keep the original visible, add your voice or read the SCRIPT below, add the HEADLINE as text.
 4. Paste the CAPTION below. Keep the credit line (Instagram also adds "Remixed from @owner" automatically).
 5. Do NOT download and re-upload the clip; do NOT remove credits or watermarks.
"""


def slugify(text: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n] or "story"


def expand_topic(topic: str) -> set[str]:
    """'cricket' also matches ODI / wicket / innings / IPL / BCCI ...: a topic word that belongs to one of the known
    topic families brings the whole family along."""
    words = {w for w in re.findall(r"[a-z0-9]+", topic.lower()) if len(w) > 2}
    for kws, _ in TOPIC_VISUALS:
        if words & kws:
            words |= kws
    return words


def build_draft(pipeline, *, category: str | None = None, topic: str | None = None, out_dir: Path | None = None) -> Path | None:
    """Write drafts/<time>_<slug>.txt for the best matching trending story; None if nothing suitable."""
    now = pipeline.now()
    history = pipeline.load_history()
    flat = [a for h in history for a in h.get("articles", [])]
    articles = filter_published(dedupe_batch(pipeline.collector()), flat)
    trends = []
    if config.TRENDS_ENABLED:
        try:
            trends = pipeline.trends_fn()
        except Exception:  # noqa: BLE001
            trends = []
    clusters = build_clusters(articles, now, trends)
    if category:
        clusters = [c for c in clusters if c["category"].lower() == category.lower()]
    if topic:
        want = expand_topic(topic)
        clusters = [c for c in clusters if want & set(re.findall(
            r"[a-z0-9]+", (c["lead"]["title"] + " " + c["lead"]["summary"]).lower()))]
    recent_cats = [h["category"] for h in history if (ts := parse_iso(h.get("published_at")))
                   and now - ts < timedelta(hours=24)]
    picks = ordered_candidates(clusters, recent_cats, {c["category"]: 1.0 for c in clusters} or config.CATEGORY_WEIGHTS,
                               min_score=config.MIN_STORY_SCORE, limit=1, skip_sensitive=config.SKIP_SENSITIVE)
    if not picks:
        return None
    cand = picks[0]
    content, mode = pipeline.writer.write_reel(cand)
    caption = compose_caption(content, cand)
    lines = [
        f"STORY ({cand['category']}, score {cand['score']:.0f}, {len(cand['sources'])} sources"
        + (f", Google trend: {cand['trend']}" if cand.get("trend") else "") + ")",
        cand["lead"]["title"], "",
        "SOURCES (read these before posting)",
        *[f"- {a['source']}: {a['url']}" for a in cand["articles"][:5]], "",
        "HEADLINE (on-screen text)", content["hook"], "",
        "SCRIPT (read it aloud, about 25 seconds)", *[f"{i}. {s}" for i, s in enumerate(content["narration"], 1)], "",
    ]
    sc = content.get("scorecard")
    if sc:
        lines += ["SCORECARD (verified against the sources)",
                  f"{sc['team_a']} {sc['score_a']}  vs  {sc['team_b']} {sc['score_b']}", sc["result"],
                  *([f"Top performer: {sc['highlight']}"] if sc.get("highlight") else []), ""]
    lines += ["CAPTION (paste as is)", caption, "", CHECKLIST]
    out_dir = Path(out_dir or config.BASE_DIR / "drafts")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{now.strftime('%Y%m%d_%H%M')}_{slugify(cand['lead']['title'])}.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
