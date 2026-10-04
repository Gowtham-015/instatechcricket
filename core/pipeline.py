"""One publishing cycle: plan -> collect -> rank -> write -> render -> host -> publish -> record.

Design rules
- Idempotent & safe: single-instance lock, slot bookkeeping, daily cap, min interval, IG quota check.
- Never double-posts: a write that may have succeeded is verified against recent media before any retry.
- Never loops forever: each story is attempted at most twice per 24h; at most N candidates per run.
- Small state: every persisted file is pruned (see config retention settings).
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from .ai_writer import AIWriter, compose_caption
from .analytics import update_insights
from .collector import collect_news
from .dedupe import dedupe_batch, filter_published, similar_to_history
from .health import Health, mask_secrets
from .image_renderer import render_post
from .instagram_client import InstagramClient, InstagramError
from .media_host import MediaHostError, get_host, wait_until_public
from .ranker import build_clusters, ordered_candidates
from .schedule import current_slot
from .storage import (LockError, ProcessLock, parse_iso, prune_dict_by_age, prune_records, read_json,
                      write_json)

log = logging.getLogger("pipeline")
MAX_ATTEMPTS_PER_STORY = 2


class Pipeline:
    def __init__(self, *, collector=collect_news, writer: AIWriter | None = None, host=None,
                 ig: InstagramClient | None = None, health: Health | None = None, now_fn=None,
                 data_dir: Path | None = None, sleep=time.sleep, verify_public=wait_until_public):
        self.data_dir = Path(data_dir or config.DATA_DIR)
        self.collector = collector
        self.writer = writer or AIWriter(cache_path=self.data_dir / "ai_cache.json")
        self._host = host
        self._ig = ig
        self.health = health or Health(self.data_dir / "health_state.json")
        self.now = now_fn or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.verify_public = verify_public
        self.history_path = self.data_dir / "history.json"
        self.schedule_path = self.data_dir / "schedule_state.json"
        self.attempts_path = self.data_dir / "attempts.json"

    # ------------------------------------------------------------- lazy deps
    @property
    def host(self):
        if self._host is None:
            self._host = get_host()
        return self._host

    @property
    def ig(self) -> InstagramClient:
        if self._ig is None:
            self._ig = InstagramClient()
        return self._ig

    # ------------------------------------------------------------- state
    def load_history(self) -> list[dict]:
        return read_json(self.history_path, [])

    def save_history(self, history: list[dict]) -> None:
        write_json(self.history_path, prune_records(history, max_records=config.HISTORY_MAX_RECORDS,
                                                    max_days=config.HISTORY_MAX_DAYS))

    # ------------------------------------------------------------- run
    def run_cycle(self, *, force: bool = False, dry_run: bool | None = None) -> dict:
        dry = config.DRY_RUN if dry_run is None else dry_run
        try:
            with ProcessLock(self.data_dir / "agent.lock"):
                return self._run_locked(force=force, dry=dry)
        except LockError as e:
            log.warning("Skipping: %s", e)
            return {"status": "skipped", "reason": "locked"}

    def _run_locked(self, *, force: bool, dry: bool) -> dict:
        self._dry = dry
        now = self.now()
        local = now.astimezone(ZoneInfo(config.TIMEZONE))
        paused = self.health.paused_until()
        if paused and not force:
            return {"status": "skipped", "reason": f"paused until {paused.isoformat()}"}

        history = self.load_history()
        sched = read_json(self.schedule_path, {"done": {}})
        done = set(sched.get("done", {}))
        slot = current_slot(local, config.POST_TIMES, config.SLOT_GRACE_MINUTES, done)
        breaking_only = False
        if not slot and not force:
            if not config.BREAKING_ENABLED:
                return {"status": "idle", "reason": "no slot due"}
            breaking_only = True

        # --- caps -------------------------------------------------------
        today = local.date()
        posts_today = [h for h in history if (ts := parse_iso(h.get("published_at")))
                       and ts.astimezone(ZoneInfo(config.TIMEZONE)).date() == today and not h.get("dry_run")]
        if len(posts_today) >= config.MAX_POSTS_PER_DAY and not force:
            return {"status": "skipped", "reason": f"daily cap {config.MAX_POSTS_PER_DAY} reached"}
        real = [h for h in history if not h.get("dry_run") and parse_iso(h.get("published_at"))]
        if real and not force:
            gap = now - max(parse_iso(h["published_at"]) for h in real)
            if gap < timedelta(minutes=config.MIN_POST_INTERVAL_MINUTES):
                return {"status": "skipped", "reason": "min interval not reached"}
        if not dry:
            try:
                used, total = self.ig.publishing_usage()
                if used >= min(total, config.IG_DAILY_API_LIMIT):
                    return {"status": "skipped", "reason": f"instagram API quota {used}/{total}"}
            except InstagramError as e:
                if e.token_problem:
                    return self._fail(f"Instagram token invalid/expired: {e}", token=True)
                log.info("Quota check unavailable (%s) - continuing", str(e)[:80])

        # --- collect & rank ----------------------------------------------
        try:
            articles = self.collector()
        except Exception as e:  # noqa: BLE001
            return self._fail(f"news collection failed: {e}")
        recent_hist = [h for h in history if (ts := parse_iso(h.get("published_at")))
                       and now - ts < timedelta(hours=48)]
        flat = [a for h in history for a in h.get("articles", [])]
        articles = filter_published(dedupe_batch(articles), flat)
        clusters = [c for c in build_clusters(articles, now)
                    if not similar_to_history(c, [a["title"] for h in recent_hist for a in h.get("articles", [])])]
        attempts = prune_dict_by_age(read_json(self.attempts_path, {}), max_entries=200, max_age_hours=24)
        exclude = {fp for fp, v in attempts.items() if v.get("count", 0) >= MAX_ATTEMPTS_PER_STORY}
        recent_cats = [h["category"] for h in history if (ts := parse_iso(h.get("published_at")))
                       and now - ts < timedelta(hours=24) and not h.get("dry_run")]
        cands = ordered_candidates(clusters, recent_cats, config.CATEGORY_WEIGHTS, min_score=config.MIN_STORY_SCORE,
                                   exclude=exclude, limit=config.MAX_PUBLISH_ATTEMPTS_PER_RUN + 2,
                                   skip_sensitive=config.SKIP_SENSITIVE)
        if breaking_only:
            cands = [c for c in cands if c["breaking"]]
        if not cands:
            return {"status": "idle", "reason": "no suitable story" + (" (breaking-only)" if breaking_only else "")}

        # --- try candidates ------------------------------------------------
        last_error = ""
        for cand in cands[: config.MAX_PUBLISH_ATTEMPTS_PER_RUN]:
            try:
                record = self._publish_candidate(cand, slot, dry, now)
            except Exception as e:  # noqa: BLE001 - every failure path is handled the same way
                last_error = mask_secrets(f"{type(e).__name__}: {e}")
                log.error("Candidate failed (%s): %s", cand["lead"]["title"][:60], last_error)
                a = attempts.get(cand["fingerprint"], {"count": 0})
                attempts[cand["fingerprint"]] = {"count": a["count"] + 1, "ts": time.time()}
                write_json(self.attempts_path, attempts)
                if isinstance(e, InstagramError) and e.token_problem:
                    return self._fail(f"Instagram token invalid/expired: {e}", token=True)
                continue
            if dry:
                return {"status": "dry_run", "title": record["title"], "files": record.get("files", [])}
            history.append(record)
            self.save_history(history)
            if slot:
                done_map = {k: v for k, v in sched.get("done", {}).items()
                            if v >= (now - timedelta(days=7)).isoformat()}
                done_map[slot] = now.isoformat()
                write_json(self.schedule_path, {"done": done_map})
            self.health.record_success()
            try:
                if update_insights(self.ig, history, now=now):
                    self.save_history(history)
            except Exception:  # noqa: BLE001 - analytics must never break publishing
                log.info("Insights refresh skipped")
            return {"status": "published", "title": record["title"], "permalink": record.get("permalink"),
                    "category": record["category"], "mode": record["mode"]}
        return self._fail(last_error or "all candidates failed")

    # ------------------------------------------------------------- helpers
    def _fail(self, message: str, token: bool = False) -> dict:
        if getattr(self, "_dry", False):  # dry runs never touch health state or send alerts
            return {"status": "failed", "error": mask_secrets(message)}
        self.health.record_failure(message)
        if token:
            self.health.alert(message, force=True)
        return {"status": "failed", "error": mask_secrets(message)}

    def _publish_candidate(self, cand: dict, slot: str | None, dry: bool, now: datetime) -> dict:
        content, mode = self.writer.write(cand)
        if mode == "fallback" and self.writer.ai_enabled:
            log.warning("AI unavailable for this story -> using extractive fallback copy")
            if not config.ALLOW_FALLBACK_POSTS:
                raise RuntimeError("AI failed and ALLOW_FALLBACK_POSTS=false")
        caption = compose_caption(content, cand)
        stem = f"{now.strftime('%Y%m%d%H%M%S')}_{cand['fingerprint'][:8]}"
        record = {
            "id": stem, "title": cand["lead"]["title"], "url": cand["lead"]["url"], "category": cand["category"],
            "fingerprint": cand["fingerprint"], "sources": cand["sources"], "score": cand["score"],
            "breaking": cand["breaking"], "mode": mode, "slot": slot, "caption_len": len(caption),
            "published_at": now.isoformat(),
            "articles": [{"url": a["url"], "title": a["title"]} for a in cand["articles"][:6]],
        }
        tmp = Path(tempfile.mkdtemp(prefix="igpost_"))
        try:
            paths = render_post(content, cand, tmp, stem, now.astimezone(ZoneInfo(config.TIMEZONE)))
            if dry:
                out = config.PREVIEW_DIR
                out.mkdir(parents=True, exist_ok=True)
                files = []
                for p in paths:
                    shutil.copyfile(p, out / p.name)
                    files.append(str(out / p.name))
                (out / f"{stem}_caption.txt").write_text(caption, encoding="utf-8")
                record.update(dry_run=True, files=files)
                return record
            hosted = []
            try:
                for p in paths:
                    hosted.append(self.host.upload(p, p.name))
                for h in hosted:
                    if not self.verify_public(h.url):
                        raise MediaHostError(f"hosted image not publicly reachable as JPEG: {h.url.split('?')[0]}")
                result = self.ig.publish_images([h.url for h in hosted], caption)
            finally:
                for h in hosted:
                    try:
                        self.host.delete(h.ref)
                    except Exception:  # noqa: BLE001
                        log.info("Could not delete hosted media (will be cleaned up later)")
            record.update(media_id=result.media_id, permalink=result.permalink)
            log.info("Published %s | %s | %s", cand["category"], record["title"][:70], result.permalink)
            return record
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
