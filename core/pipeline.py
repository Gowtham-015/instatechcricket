"""One publishing cycle: plan -> collect -> rank -> write -> render -> host -> publish -> record.

Design rules
- Idempotent & safe: single-instance lock, slot bookkeeping, daily cap, min interval, IG quota check.
- Never double-posts: a write that may have succeeded is verified against recent media before any retry.
- Never loops forever: each story is attempted at most twice per 24h; at most N candidates per run.
- Small state: every persisted file is pruned (see config retention settings).
"""
from __future__ import annotations

import inspect
import json
import logging
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from .ai_writer import AIWriter, UnfocusedStoryError, compose_caption, compose_link_message
from .analytics import update_insights
from . import collector as collector_mod
from . import editorial, story
from .collector import collect_news
from .commons import Photo
from .editorial import Decision
from .media_match import MediaRetriever
from .photo_post import render_fact_card, render_photo_carousel, render_photo_post
from . import publishable
from .provenance import MediaAsset
from .video_post import prepare_original_video
from .dedupe import dedupe_batch, filter_published, similar_to_history
from .facebook_client import FacebookPageClient
from .health import Health, mask_secrets
from .image_renderer import REEL, render_post, render_slides
from .instagram_client import InstagramClient, InstagramError
from .media_host import MediaHostError, get_host, wait_until_public
from .ranker import build_clusters, ordered_candidates, pick_format
from .trends import fetch_trend_report
from .schedule import current_slot
from .reel_studio import make_reel
from . import variety
from .video_renderer import VideoError, encode_slideshow, encoder_available
from .storage import (LockError, ProcessLock, parse_iso, prune_dict_by_age, prune_records, read_json,
                      write_json)

log = logging.getLogger("pipeline")
MAX_ATTEMPTS_PER_STORY = 2
AMBIGUOUS_WINDOW_HOURS = 6


class HeldStoryError(RuntimeError):
    """The editorial rules say this story must wait (thin sourcing, conflicting figures). Not a failure."""


class AmbiguousPublishError(RuntimeError):
    """Instagram did not confirm whether the post went live. Nothing may be re-published until it is reconciled."""


class Pipeline:
    def __init__(self, *, collector=collect_news, writer: AIWriter | None = None, host=None,
                 ig: InstagramClient | None = None, health: Health | None = None, now_fn=None,
                 data_dir: Path | None = None, sleep=time.sleep, verify_public=wait_until_public,
                 reel_maker=make_reel, trends_fn=fetch_trend_report, fb=None, retriever=None):
        self.data_dir = Path(data_dir or config.DATA_DIR)
        self.collector = collector
        self.writer = writer or AIWriter(cache_path=self.data_dir / "ai_cache.json")
        self._host = host
        self._ig = ig
        self.now = now_fn or (lambda: datetime.now(timezone.utc))
        self.health = health or Health(self.data_dir / "health_state.json", now_fn=self.now)
        self.sleep = sleep
        self.verify_public = verify_public
        self.reel_maker = reel_maker
        self.trends_fn = trends_fn
        self._fb = fb          # None = build from config when configured, False = disabled
        self._retriever = retriever
        self._trend_report = None
        self._collector_report: dict = {}
        self.pending_path = self.data_dir / "pending_publish.json"
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

    @property
    def fb(self):
        if self._fb is False:
            return None
        if self._fb is None and config.FB_PAGE_ID and config.FB_PAGE_ACCESS_TOKEN and config.FB_MODE != "off":
            self._fb = FacebookPageClient()
        return self._fb or None

    @property
    def retriever(self) -> MediaRetriever:
        if self._retriever is None:
            self._retriever = MediaRetriever()
        return self._retriever

    def facebook_publish(self, fmt: str, urls: list[str], caption: str, content: dict, cand: dict) -> dict | None:
        """Same content on the connected Facebook Page. NEVER raises: Instagram already succeeded at this point."""
        fb = self.fb
        if fb is None or config.FB_MODE == "off":
            return None
        out: dict = {}
        url = cand["lead"].get("url")
        failure: Exception | None = None

        def err(e: Exception) -> str:
            return mask_secrets(f"{type(e).__name__}: {e}")[:200]

        if config.FB_MODE in ("crosspost", "both"):
            try:
                r = fb.post_reel(urls[0], caption) if fmt == "reel" else fb.post_photos(urls, caption)
                out["crosspost"] = r["permalink"]
            except Exception as e:  # noqa: BLE001
                failure = e
                out["error"] = err(e)
                log.warning("Facebook %s failed (Instagram post is fine): %s", fmt, out["error"])
        want_link = config.FB_MODE in ("link", "both")
        # A refused (not merely slow) upload would leave Facebook with nothing: send the link post instead.
        refused = failure is not None and not getattr(failure, "transient", False)
        if not want_link and fmt == "reel" and "crosspost" not in out and refused:
            want_link = True
            out["fallback"] = "link"
        if want_link and url:
            try:
                out["link"] = fb.post_link(compose_link_message(content, cand), url)["permalink"]
            except Exception as e2:  # noqa: BLE001
                out["error"] = (out.get("error", "") + " | " if out.get("error") else "") + err(e2)
                log.warning("Facebook link post failed (Instagram post is fine): %s", err(e2))
        log.info("Facebook: %s", out)
        return out

    # ------------------------------------------------------------- state
    def load_history(self) -> list[dict]:
        return read_json(self.history_path, [])

    def save_history(self, history: list[dict]) -> None:
        write_json(self.history_path, prune_records(history, max_records=config.HISTORY_MAX_RECORDS,
                                                    max_days=config.HISTORY_MAX_DAYS))

    # ------------------------------------------------------------- run
    def run_cycle(self, *, force: bool = False, dry_run: bool | None = None, fmt: str | None = None) -> dict:
        dry = config.DRY_RUN if dry_run is None else dry_run
        try:
            with ProcessLock(self.data_dir / "agent.lock"):
                return self._run_locked(force=force, dry=dry, fmt=fmt)
        except LockError as e:
            log.warning("Skipping: %s", e)
            return {"status": "skipped", "reason": "locked"}

    def _run_locked(self, *, force: bool, dry: bool, fmt: str | None = None) -> dict:
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

        if not dry:
            history = self._reconcile_pending(history, now)
        pending_fps = {e.get("fingerprint") for e in read_json(self.pending_path, [])}

        # --- collect & rank ----------------------------------------------
        try:
            articles = self.collector()
            self._collector_report = dict(collector_mod.LAST_REPORT) if self.collector is collect_news else {}
        except Exception as e:  # noqa: BLE001
            return self._fail(f"news collection failed: {e}")
        recent_hist = [h for h in history if (ts := parse_iso(h.get("published_at")))
                       and now - ts < timedelta(hours=48)]
        flat = [a for h in history for a in h.get("articles", [])]
        articles = filter_published(dedupe_batch(articles), flat)
        trends = []
        self._trend_report = None
        if config.TRENDS_ENABLED:
            try:
                trends = self.trends_fn()
            except Exception as e:  # noqa: BLE001 - trending is a bonus signal only
                log.info("Trend lookup failed (%s) - ranking on the other signals", type(e).__name__)
                trends = []
            if hasattr(trends, "providers"):
                self._trend_report = trends
                log.info("Trend providers -> %s", trends.describe())
        clusters = [c for c in build_clusters(articles, now, trends)
                    if not similar_to_history(c, [a["title"] for h in recent_hist for a in h.get("articles", [])])]
        attempts = prune_dict_by_age(read_json(self.attempts_path, {}), max_entries=200, max_age_hours=24)
        exclude = {fp for fp, v in attempts.items() if v.get("count", 0) >= MAX_ATTEMPTS_PER_STORY} | pending_fps
        recent_cats = [h["category"] for h in history if (ts := parse_iso(h.get("published_at")))
                       and now - ts < timedelta(hours=24) and not h.get("dry_run")]
        weights = dict(config.CONTENT_FORMATS)
        if "reel" in weights and not encoder_available():
            log.warning("ffmpeg not available -> Reels disabled for this run (pip install imageio-ffmpeg)")
            weights.pop("reel")
        recent_fmts = [h.get("format", "carousel") for h in history if (ts := parse_iso(h.get("published_at")))
                       and now - ts < timedelta(hours=24) and not h.get("dry_run")]
        if config.FORMAT_POLICY == "legacy":
            chosen_fmt = fmt or pick_format(recent_fmts, weights or {"carousel": 1})
        else:   # editorial: the story and the media found decide; only an explicit --format overrides
            chosen_fmt = fmt
        cands = ordered_candidates(clusters, recent_cats, config.CATEGORY_WEIGHTS, min_score=config.MIN_STORY_SCORE,
                                   exclude=exclude, limit=config.MAX_PUBLISH_ATTEMPTS_PER_RUN + 2,
                                   skip_sensitive=config.SKIP_SENSITIVE)
        if breaking_only:
            cands = [c for c in cands if c["breaking"]]
        for c in cands[:3]:
            log.info("Candidate: %s | %s", c["lead"]["title"][:70], c.get("why") or f"score {c['score']:.0f}")
        if not cands:
            return {"status": "idle", "reason": "no suitable story" + (" (breaking-only)" if breaking_only else "")}

        # --- try candidates ------------------------------------------------
        last_error = ""
        held: list[str] = []
        for cand in cands[: config.MAX_PUBLISH_ATTEMPTS_PER_RUN]:
            try:
                record = self._publish_candidate(cand, slot, dry, now, chosen_fmt)
            except AmbiguousPublishError as e:
                # Instagram may or may not have published: stop the run, never try another post until reconciled.
                attempts[cand["fingerprint"]] = {"count": MAX_ATTEMPTS_PER_STORY, "ts": time.time()}
                write_json(self.attempts_path, attempts)
                return self._fail(f"ambiguous publish result, will reconcile next run: {e}")
            except HeldStoryError as e:
                log.warning("Story held back (%s): %s", cand["lead"]["title"][:60], e)
                last_error = f"held: {e}"
                held.append(last_error)
                continue                                  # no attempt is counted: more sources may arrive later
            except Exception as e:  # noqa: BLE001 - every failure path is handled the same way
                last_error = mask_secrets(f"{type(e).__name__}: {e}")
                (log.warning if isinstance(e, UnfocusedStoryError) else log.error)(
                    "Candidate skipped (%s): %s", cand["lead"]["title"][:60], last_error)
                if cand.get("trend"):
                    log.info("(it matched Google trend '%s')", cand["trend"])
                a = attempts.get(cand["fingerprint"], {"count": 0})
                n_att = MAX_ATTEMPTS_PER_STORY if isinstance(e, UnfocusedStoryError) else a["count"] + 1
                attempts[cand["fingerprint"]] = {"count": n_att, "ts": time.time()}
                write_json(self.attempts_path, attempts)
                if isinstance(e, InstagramError) and e.token_problem:
                    return self._fail(f"Instagram token invalid/expired: {e}", token=True)
                continue
            if dry:
                return {"status": "dry_run", "format": record["format"], "title": record["title"],
                        "files": record.get("files", []), "decision": record.get("decision"),
                        "report": record.pop("_report", "")}
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
            return {"status": "published", "format": record["format"], "title": record["title"],
                    "permalink": record.get("permalink"), "category": record["category"], "mode": record["mode"],
                    "decision": record.get("decision")}
        if held and len(held) == len(cands[: config.MAX_PUBLISH_ATTEMPTS_PER_RUN]):
            return {"status": "idle", "reason": "every candidate is held by the editorial rules: " + held[0]}
        return self._fail(last_error or "all candidates failed")

    # ------------------------------------------------------------- helpers
    def _fail(self, message: str, token: bool = False) -> dict:
        if getattr(self, "_dry", False):  # dry runs never touch health state or send alerts
            return {"status": "failed", "error": mask_secrets(message)}
        self.health.record_failure(message)
        if token:
            self.health.alert(message, force=True)
        return {"status": "failed", "error": mask_secrets(message)}

    def _publish_candidate(self, cand: dict, slot: str | None, dry: bool, now: datetime,
                           fmt: str | None = None) -> dict:
        if config.FORMAT_POLICY == "legacy":
            return self._publish_legacy(cand, slot, dry, now, fmt or "carousel")
        return self._publish_editorial(cand, slot, dry, now, fmt)

    # ------------------------------------------------------------- editorial path
    def _publish_editorial(self, cand: dict, slot: str | None, dry: bool, now: datetime, forced: str | None) -> dict:
        stem = f"{now.strftime('%Y%m%d%H%M%S')}_{cand['fingerprint'][:8]}"
        local_now = now.astimezone(ZoneInfo(config.TIMEZONE))
        tmp = Path(tempfile.mkdtemp(prefix="igpost_"))
        rec: dict | None = None
        try:
            profile = story.analyse(cand, now)
            cand["editor_notes"] = story.writer_guidance(profile)
            plan = self.retriever.retrieve(cand, profile, tmp, None, now)
            for r in plan.rejected[:6]:
                log.info("Rejected %s '%s' (%s): %s", r.kind, r.title[:50], r.source, r.reason)
            avoid: set[str] = set()
            content = mode = built = decision = None
            for _ in range(5):
                decision = editorial.decide(profile, plan, encoder_ok=encoder_available(),
                                            tts_ok=config.TTS_PROVIDER != "none", forced=forced, avoid=avoid)
                rec = editorial.build_record(cand, profile, plan, decision, trend_report=self._trend_report,
                                             collector_report=self._collector_report)
                log.info("%s", decision.explanation())
                if decision.format == "hold":
                    rec["outcome"] = {"published": False, "held": True}
                    self._log_decision(rec, dry)
                    raise HeldStoryError(decision.reason)
                try:
                    if decision.format != "reel" and content is None:
                        content, mode = self.writer.write(cand)
                        self._warn_if_fallback(mode)
                    built = self._render(decision, cand, content, tmp, stem, local_now)
                    publishable.validate(built["files"], decision.format)       # rendered is not the same as publishable
                    break
                except UnfocusedStoryError:
                    raise
                except Exception as e:  # noqa: BLE001 - any render/encode problem means 'use a safer format'
                    log.warning("%s could not be built (%s: %s) -> falling back", decision.format, type(e).__name__,
                                mask_secrets(str(e))[:160])
                    avoid.add(decision.format)
                    if decision.format == "card":
                        raise
            else:
                raise RuntimeError("no format could be rendered")
            content, mode, files = built["content"], built["mode"], built["files"]
            final_fmt = decision.format
            caption = compose_caption(content, cand, built.get("footage_credit"), built.get("images_credit"),
                                      built.get("notes"))
            decision.media_use = built.get("media_use", decision.media_use)
            rec["decision"]["media_use"] = decision.media_use
            record = {
                "id": stem, "title": cand["lead"]["title"], "url": cand["lead"]["url"], "category": cand["category"],
                "fingerprint": cand["fingerprint"], "sources": cand["sources"], "score": cand["score"],
                "breaking": cand["breaking"], "mode": mode, "slot": slot, "caption_len": len(caption),
                "published_at": now.isoformat(), "format": final_fmt, "reel": built.get("reel_info", ""),
                "media_use": decision.media_use, "decision": decision.reason,
                "articles": [{"url": a["url"], "title": a["title"]} for a in cand["articles"][:6]],
            }
            rec["outcome"] = {"rendered_format": final_fmt, "files": [Path(f).name for f in files]}
            try:
                out = self._deliver(record=record, files=files, fmt=final_fmt, caption=caption, content=content,
                                    cand=cand, dry=dry, reel_style=built.get("reel_style"),
                                    alt_text=built.get("alt_text"), decision_rec=rec)
            except AmbiguousPublishError:
                rec["outcome"].update(published=False, ambiguous=True)
                self._log_decision(rec, dry)
                raise
            rec["outcome"].update(published=not dry, dry_run=dry, final_format=final_fmt,
                                  uploaded_media_types=out.get("media_types"), permalink=out.get("permalink"),
                                  facebook=out.get("facebook"))
            self._log_decision(rec, dry)
            if dry:
                out["_report"] = editorial.format_report(rec)
            return out
        except HeldStoryError:
            raise
        except Exception as e:
            if rec is not None and not rec.get("outcome", {}).get("logged"):
                rec.setdefault("outcome", {}).update(published=False, error=mask_secrets(f"{type(e).__name__}: {e}")[:200])
                self._log_decision(rec, dry)
            raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _log_decision(self, rec: dict, dry: bool) -> None:
        rec["outcome"]["logged"] = True
        try:
            editorial.append_decision(self.data_dir, {**rec, "dry_run": dry, "at": self.now().isoformat()})
        except OSError:
            log.info("Could not write the decision log")

    def _call_reel_maker(self, content: dict, cand: dict, out: Path, **new_kw):
        """Pass the editorial arguments only to reel makers that accept them (test doubles may not)."""
        kw = {"music_path": config.MUSIC_PATH or None}
        try:
            params = inspect.signature(self.reel_maker).parameters
            var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        except (TypeError, ValueError):
            params, var_kw = {}, False
        kw.update({k: v for k, v in new_kw.items() if var_kw or k in params})
        return self.reel_maker(content, cand, out, **kw)

    @staticmethod
    def _as_photo(a: MediaAsset) -> Photo:
        return Photo(Path(a.path), a.attribution or a.creator or a.source, a.title, "", a.source,
                     a.attribution_required, a.caption_credit)

    def _render(self, d: Decision, cand: dict, content: dict | None, tmp: Path, stem: str, local_now: datetime) -> dict:
        """Render the chosen format. Raises VideoError/OSError/ValueError on render problems (caller falls back)."""
        notes: list[str] = []
        file_photo = any(p.role != "event" for p in d.photos)
        if d.format == "video":
            v = d.video
            credit = v.caption_credit or v.attribution or v.creator or v.source
            path = prepare_original_video(v, tmp / f"{stem}.mp4", credit_text=f"Video: {credit}")
            content, mode = self.writer.write(cand) if content is None else (content, "ai")
            notes.append(f"🎥 Original footage: {credit}" + (" (original sound)" if v.has_audio else ""))
            return {"files": [path], "content": content, "mode": mode, "notes": notes, "media_use": "event_media",
                    "images_credit": None, "reel_info": f"original-video audio={v.has_audio}",
                    "alt_text": None}
        if d.format == "photo":
            files = render_photo_post(content, cand, d.photos[0], tmp, stem, local_now)
            if file_photo:
                notes.append("🖼️ File photo: shows the subject of this story, not shown to be from this event.")
            return {"files": files, "content": content, "mode": "ai", "notes": notes,
                    "images_credit": editorial.credits_line(d.photos[:1]), "media_use": editorial._media_use(d.photos[:1]),
                    "alt_text": f"{d.photos[0].title[:150]}"}
        if d.format == "carousel":
            if d.photos:
                use = d.photos[:4]
                files = render_photo_carousel(content, cand, use, tmp, stem, local_now)
                if any(p.role != "event" for p in use):
                    notes.append("🖼️ Some photos are file photos of the subjects, not shown to be from this event.")
                return {"files": files, "content": content, "mode": "ai", "notes": notes,
                        "images_credit": editorial.credits_line(use), "media_use": editorial._media_use(use)}
            return {"files": render_post(content, cand, tmp, stem, local_now), "content": content, "mode": "ai",
                    "notes": notes, "images_credit": None, "media_use": "editorial_graphic"}
        if d.format == "card":
            return {"files": render_fact_card(content, cand, tmp, stem, local_now), "content": content, "mode": "ai",
                    "notes": notes, "images_credit": None, "media_use": "editorial_graphic"}
        # ---- reel
        if config.REEL_STYLE == "dynamic" and config.REEL_VARIETY:
            style = variety.pick_style(str(cand.get("fingerprint", "")), variety.load_recent(self.data_dir))
            cand["reel_style"], cand["style_hint"] = style.to_dict(), variety.opener_hint(style)
        if content is None or "narration" not in content:
            content, mode = self.writer.write_reel(cand)
            self._warn_if_fallback(mode)
        else:
            mode = "ai"
        photos = [self._as_photo(a) for a in d.photos[:4]]
        res = self._call_reel_maker(content, cand, tmp / f"{stem}.mp4", authentic_photos=photos,
                                    stock_ok=d.illustrative_footage, voiceover=d.voiceover)
        stock = res.footage in ("pexels", "pixabay", "local")
        if stock:
            notes.append("🎞️ Illustrative footage: generic stock clips, not from the event itself.")
        if file_photo:
            notes.append("🖼️ Some photos are file photos of the subjects, not shown to be from this event.")
        use = ("licensed_illustrative" if stock and not photos else
               editorial._media_use(d.photos[:4]) if photos else "editorial_graphic")
        info = f"voice={res.voiced} footage={res.footage}" + (" +photos" if res.images_credit else "")
        if res.style:
            info += f" look={res.style.get('name', '')}"
        return {"files": [res.path], "content": content, "mode": mode, "notes": notes, "reel_style": res.style,
                "images_credit": res.images_credit, "footage_credit": res.credit if (config.FOOTAGE_CREDIT and stock) else None,
                "media_use": use, "reel_info": info}

    # ------------------------------------------------------------- delivery (shared by both policies)
    def _deliver(self, *, record: dict, files: list[Path], fmt: str, caption: str, content: dict, cand: dict, dry: bool,
                 reel_style: dict | None = None, alt_text: str | None = None, decision_rec: dict | None = None) -> dict:
        video = fmt in ("reel", "video")
        kinds = ["video/mp4" if Path(p).suffix.lower() == ".mp4" else "image/jpeg" for p in files]
        expected = "video/mp4" if video else "image/jpeg"
        if any(k != expected for k in kinds) or (video and len(files) != 1) or (not video and not 1 <= len(files) <= 10):
            raise RuntimeError(f"format '{fmt}' does not match the rendered media {kinds}")
        record["media_types"] = kinds
        if dry:
            out_dir = config.PREVIEW_DIR
            out_dir.mkdir(parents=True, exist_ok=True)
            copied = []
            for p in files:
                shutil.copyfile(p, out_dir / p.name)
                copied.append(str(out_dir / p.name))
            (out_dir / f"{record['id']}_caption.txt").write_text(caption, encoding="utf-8")
            if decision_rec is not None:
                (out_dir / f"{record['id']}_decision.json").write_text(
                    json.dumps(decision_rec, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            record.update(dry_run=True, files=copied)
            return record
        hosted = []
        try:
            for p in files:
                hosted.append(self.host.upload(p, Path(p).name))
            kind = "video" if video else "image"
            for h in hosted:
                if not self._reachable(h.url, kind):
                    raise MediaHostError(f"hosted {kind} not publicly reachable: {h.url.split('?')[0]}")
            try:
                if video:
                    result = self.ig.publish_reel(hosted[0].url, caption)
                    if reel_style and fmt == "reel":
                        variety.remember(self.data_dir, variety.ReelStyle.from_dict(reel_style))
                elif alt_text and len(hosted) == 1:
                    result = self.ig.publish_images([h.url for h in hosted], caption, alt_text=alt_text)
                else:
                    result = self.ig.publish_images([h.url for h in hosted], caption)
            except InstagramError as e:
                if e.ambiguous:
                    self._note_pending(record, caption)
                    raise AmbiguousPublishError(mask_secrets(str(e))[:160]) from e
                raise
            record["facebook"] = self.facebook_publish("reel" if video else "photos", [h.url for h in hosted],
                                                       caption, content, cand)
        finally:
            for h in hosted:
                try:
                    self.host.delete(h.ref)
                except Exception:  # noqa: BLE001
                    log.info("Could not delete hosted media (will be cleaned up later)")
        record.update(media_id=result.media_id, permalink=result.permalink)
        log.info("Published %s (%s) | %s | %s", cand["category"], fmt, record["title"][:70], result.permalink)
        return record

    # ------------------------------------------------------------- ambiguous publish recovery
    def _note_pending(self, record: dict, caption: str) -> None:
        pend = read_json(self.pending_path, [])
        pend.append({"fingerprint": record["fingerprint"], "caption": caption[:80], "ts": self.now().isoformat(),
                     "record": {k: v for k, v in record.items() if k not in ("files",)}})
        write_json(self.pending_path, pend[-10:])

    def _reconcile_pending(self, history: list[dict], now: datetime) -> list[dict]:
        """A previous publish whose outcome Instagram never confirmed: find it on the account and record it (so the
        story is never posted twice), or forget it once it is clearly not coming."""
        pend = read_json(self.pending_path, [])
        if not pend:
            return history
        keep, changed = [], False
        for e in pend:
            try:
                found = self.ig.find_published_by_caption(e["caption"])
            except Exception:  # noqa: BLE001
                keep.append(e)
                continue
            age = now - (parse_iso(e["ts"]) or now)
            if found:
                rec = dict(e["record"], media_id=found.media_id, permalink=found.permalink, reconciled=True)
                history.append(rec)
                changed = True
                log.warning("Reconciled an earlier ambiguous publish: it is live at %s", found.permalink)
            elif age < timedelta(hours=AMBIGUOUS_WINDOW_HOURS):
                keep.append(e)
        write_json(self.pending_path, keep)
        if changed:
            self.save_history(history)
        return history

    # ------------------------------------------------------------- legacy path (FORMAT_POLICY=legacy)
    def _publish_legacy(self, cand: dict, slot: str | None, dry: bool, now: datetime, fmt: str = "carousel") -> dict:
        stem = f"{now.strftime('%Y%m%d%H%M%S')}_{cand['fingerprint'][:8]}"
        local_now = now.astimezone(ZoneInfo(config.TIMEZONE))
        tmp = Path(tempfile.mkdtemp(prefix="igpost_"))
        try:
            files: list[Path] = []
            content, mode, credit, images, reel_info = None, "fallback", None, None, ""
            reel_style = None
            if fmt == "reel":
                try:
                    if config.REEL_STYLE == "dynamic":
                        if config.REEL_VARIETY:   # a look that differs from the last Reels (same story -> same look)
                            style = variety.pick_style(str(cand.get("fingerprint", "")), variety.load_recent(self.data_dir))
                            cand["reel_style"], cand["style_hint"] = style.to_dict(), variety.opener_hint(style)
                        content, mode = self.writer.write_reel(cand)
                        self._warn_if_fallback(mode)
                        res = self.reel_maker(content, cand, tmp / f"{stem}.mp4", music_path=config.MUSIC_PATH or None)
                        files = [res.path]
                        reel_info = f"voice={res.voiced} footage={res.footage}" + (" +photos" if res.images_credit else "")
                        if res.style:
                            reel_info += f" look={res.style.get('name', '')}"
                            reel_style = res.style
                        images = res.images_credit
                        credit = res.credit if config.FOOTAGE_CREDIT else None
                    else:  # legacy "cards" style: info cards as a slideshow video
                        content, mode = self.writer.write(cand)
                        self._warn_if_fallback(mode)
                        files = [encode_slideshow(render_slides(content, cand, local_now, REEL), tmp / f"{stem}.mp4",
                                                  music_path=config.MUSIC_PATH or None)]
                except UnfocusedStoryError:
                    raise                          # a roundup is skipped entirely, not turned into a carousel
                except Exception as e:  # noqa: BLE001 - never lose the post just because video failed
                    log.warning("Reel could not be built (%s: %s) -> publishing a carousel instead",
                                type(e).__name__, mask_secrets(str(e))[:160])
                    fmt, files, content = "carousel", [], None
            if fmt != "reel":
                content, mode = self.writer.write(cand)
                self._warn_if_fallback(mode)
                files = render_post(content, cand, tmp, stem, local_now)
            caption = compose_caption(content, cand, credit, images)
            record = {
                "id": stem, "title": cand["lead"]["title"], "url": cand["lead"]["url"], "category": cand["category"],
                "fingerprint": cand["fingerprint"], "sources": cand["sources"], "score": cand["score"],
                "breaking": cand["breaking"], "mode": mode, "slot": slot, "caption_len": len(caption),
                "published_at": now.isoformat(), "format": fmt, "reel": reel_info,
                "articles": [{"url": a["url"], "title": a["title"]} for a in cand["articles"][:6]],
            }
            return self._deliver(record=record, files=files, fmt=fmt, caption=caption, content=content, cand=cand,
                                 dry=dry, reel_style=reel_style)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _warn_if_fallback(self, mode: str) -> None:
        if mode == "fallback" and self.writer.ai_enabled:
            log.warning("AI unavailable for this story -> using extractive fallback copy")
            if not config.ALLOW_FALLBACK_POSTS:
                raise RuntimeError("AI failed and ALLOW_FALLBACK_POSTS=false")

    def _reachable(self, url: str, kind: str) -> bool:
        try:
            return self.verify_public(url, kind=kind)
        except TypeError:  # injected test double without the `kind` parameter
            return self.verify_public(url)
