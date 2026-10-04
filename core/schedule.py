"""Slot-based posting plan: post once per configured time, tolerant of delayed cron runs."""
from __future__ import annotations

from datetime import datetime, timedelta


def parse_times(times: list[str]) -> list[tuple[int, int]]:
    out = []
    for t in times:
        h, m = t.split(":")
        out.append((int(h), int(m)))
    return sorted(out)


def current_slot(now_local: datetime, times: list[str], grace_minutes: int, done: set[str]) -> str | None:
    """Latest slot whose window [slot, slot+grace) contains now and which is not done yet."""
    best = None
    for day_offset in (0, -1):  # -1 handles windows that cross midnight
        day = (now_local + timedelta(days=day_offset)).replace(second=0, microsecond=0)
        for h, m in parse_times(times):
            start = day.replace(hour=h, minute=m)
            if start <= now_local < start + timedelta(minutes=grace_minutes):
                key = start.strftime("%Y-%m-%d %H:%M")
                if key not in done and (best is None or key > best):
                    best = key
    return best
