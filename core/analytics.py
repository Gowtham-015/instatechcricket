"""Lightweight analytics: pull Instagram insights for older posts and print a performance report."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .instagram_client import InstagramClient, InstagramError
from .storage import parse_iso

log = logging.getLogger("analytics")


def update_insights(ig: InstagramClient, history: list[dict], *, min_age_hours: int = 24, max_calls: int = 5,
                    now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    done = 0
    for rec in reversed(history):
        if done >= max_calls:
            break
        ts = parse_iso(rec.get("published_at"))
        if rec.get("insights") is not None or not rec.get("media_id") or not ts:
            continue
        if now - ts < timedelta(hours=min_age_hours) or now - ts > timedelta(days=14):
            continue
        try:
            rec["insights"] = ig.insights(rec["media_id"])
            done += 1
        except InstagramError as e:
            log.info("Insights unavailable for %s: %s", rec["media_id"], str(e)[:80])
            rec["insights"] = {}
    return done


def report(history: list[dict]) -> str:
    if not history:
        return "No posts yet."
    by_cat: dict[str, list[dict]] = {}
    for r in history:
        by_cat.setdefault(r.get("category", "?"), []).append(r)
    lines = [f"Total posts tracked: {len(history)}", "", "Posts by category:"]
    for cat, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        share = 100 * len(items) / len(history)
        with_ins = [i["insights"] for i in items if i.get("insights")]
        reach = sum(x.get("reach", 0) for x in with_ins) / len(with_ins) if with_ins else 0
        lines.append(f"  {cat:<14} {len(items):>3} posts ({share:4.1f}%)   avg reach: {reach:,.0f}")
    scored = [r for r in history if r.get("insights")]
    if scored:
        best = max(scored, key=lambda r: r["insights"].get("total_interactions", 0))
        lines += ["", f"Top post by interactions: {best['title'][:70]}",
                  f"  {best.get('permalink', '')}  {best['insights']}"]
    modes: dict[str, int] = {}
    for r in history:
        modes[r.get("mode", "?")] = modes.get(r.get("mode", "?"), 0) + 1
    lines += ["", "Copy source: " + ", ".join(f"{k}={v}" for k, v in sorted(modes.items()))]
    return "\n".join(lines)
