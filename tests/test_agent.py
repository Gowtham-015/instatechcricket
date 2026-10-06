"""Offline test-suite (no network, no real credentials). Run:  python -m unittest discover -s tests -v"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import requests
from PIL import Image

from core import (clip_post, ai_writer, config, dedupe, health, image_renderer, media_host, ranker, reel_renderer, reel_studio,
                  schedule, stock, storage, tts, video_renderer)
from core.ai_writer import AIWriter, ContentError
from core.instagram_client import InstagramClient, InstagramError
from core.pipeline import Pipeline

ROOT = Path(__file__).resolve().parent.parent
IST = ZoneInfo("Asia/Kolkata")


# ------------------------------------------------------------------ fakes
class Resp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data if data is not None else {}, headers or {}

    def json(self):
        return self._data

    def close(self):
        pass


class FakeIG:
    """Routes calls like the Graph API. `overrides['METHOD:path']` = list of Resp/Exception to pop first."""

    def __init__(self, uid="U"):
        self.uid, self.calls, self.overrides, self.status = uid, [], {}, {}
        self.n_child, self.last_caption, self.published = 0, "", 0

    def _pop(self, key):
        q = self.overrides.get(key)
        if q:
            item = q.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return None

    def post(self, url, data=None, timeout=None, **kw):
        path = url.split("/v26.0/")[1]
        self.calls.append(("POST", path, dict(data or {})))
        o = self._pop(f"POST:{path}")
        if o:
            return o
        if path == f"{self.uid}/media":
            if data.get("is_carousel_item"):
                self.n_child += 1
                return Resp(200, {"id": f"c{self.n_child}"})
            self.last_caption = data.get("caption", "")
            if data.get("media_type") == "REELS":
                return Resp(200, {"id": "reel1"})
            return Resp(200, {"id": "parent" if data.get("media_type") == "CAROUSEL" else "single"})
        if path == f"{self.uid}/media_publish":
            self.published += 1
            return Resp(200, {"id": "m1"})
        return Resp(404, {"error": {"message": "unknown"}})

    def get(self, url, params=None, timeout=None, **kw):
        path = url.split("/v26.0/")[1]
        self.calls.append(("GET", path, dict(params or {})))
        o = self._pop(f"GET:{path}")
        if o:
            return o
        if path == self.uid:
            return Resp(200, {"username": "dailypulse"})
        if path.endswith("content_publishing_limit"):
            return Resp(200, {"data": [{"quota_usage": 1, "config": {"quota_total": 100}}]})
        if path.endswith("/media"):
            return Resp(200, {"data": []})
        if path == "m1":
            return Resp(200, {"permalink": "https://instagram.com/p/abc/"})
        if path.endswith("/insights"):
            return Resp(200, {"data": [{"name": "reach", "values": [{"value": 500}]}]})
        return Resp(200, {"status_code": self.status.get(path, "FINISHED")})


def make_ig(session=None, **kw):
    return InstagramClient("U", "TOKEN_SECRET_123", "https://graph.facebook.com", "v26.0",
                           session=session or FakeIG(), sleep=lambda s: None, **kw)


def make_article(i, category="News", source="BBC News", age_min=30, now=None):
    now = now or datetime.now(timezone.utc)
    return {
        "id": f"id{i}", "title": f"Story{i:03d}x{category.lower()} wxq{i} zlk{i} mnb{i} plo{i} headline today",
        "summary": f"Details about story {i} wxq{i} zlk{i}. The officials confirmed the plan to expand coverage across regions. "
                   f"More context for mnb{i} plo{i} follows in this second sentence of the summary.",
        "url": f"https://example.com/{category}/{i}", "source": source, "category": category,
        "published": (now - timedelta(minutes=age_min)).isoformat(),
    }


def make_cluster(category="Technology", n_sources=2):
    arts = [make_article(1, category, s) for s in ["TechCrunch", "The Verge", "Wired"][:n_sources]]
    for i, a in enumerate(arts):
        a["title"] = "Apple unveils new chip for thin laptops with longer battery life"
        a["summary"] = ("Apple announced a new processor on Tuesday. The chip targets thin laptops and promises longer "
                        "battery life for students and remote workers. Pricing starts later this year.")
        a["url"] = f"https://example.com/t/{i}"
    return {"fingerprint": "fp1", "category": category, "articles": arts, "lead": arts[0],
            "sources": [a["source"] for a in arts], "breaking": False, "score": 70.0, "age_hours": 0.5}


VALID = {
    "hook": "Apple unveils a new chip built for thin laptops",
    "slides": [
        {"heading": "What happened", "body": "Apple announced a new processor aimed at thin laptops with better efficiency."},
        {"heading": "Who it targets", "body": "The chip is designed for students and remote workers who need longer battery life."},
        {"heading": "When it arrives", "body": "Pricing details will be shared later this year according to the announcement."},
    ],
    "why_it_matters": "Longer battery life means fewer charging breaks for students and remote workers on thin laptops.",
    "caption": "Apple has announced a new processor for thin laptops. The company says it targets students and remote workers "
               "who want longer battery life.\n\nPricing details are expected later this year, so buyers can plan ahead.",
    "hashtags": ["#Apple", "chips", "Tech News!", "#laptops"],
}


REEL_VALID = {
    "hook": "Apple unveils a new chip built for thin laptops",
    "narration": [
        "Apple just announced a brand new processor for thin laptops.",
        "The company says it is built for students and remote workers.",
        "The big promise is longer battery life during a normal day.",
        "Pricing details will be shared later this year.",
    ],
    "broll_queries": ["Laptop Typing!", "technology circuit board", "laptop typing"],
    "why_it_matters": "Longer battery life means fewer charging breaks for students and remote workers on thin laptops.",
    "caption": "Apple has announced a new processor for thin laptops. The company says it targets students and remote workers "
               "who want longer battery life.\n\nPricing details are expected later this year, so buyers can plan ahead.",
    "hashtags": ["#Apple", "chips", "#laptops"],
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        p = mock.patch.multiple(config, DATA_DIR=self.tmp, PREVIEW_DIR=self.tmp / "preview",
                                CONTENT_FORMATS={"carousel": 1}, VIDEO_SECONDS_PER_SLIDE=0.6, VIDEO_FPS=10,
                                REEL_STYLE="cards", PEXELS_API_KEY="", PIXABAY_API_KEY="", STOCK_PROVIDER="auto", TTS_PROVIDER="none", BROLL_DIR="", MUSIC_PATH="",
                                ACCOUNT_HANDLE="@dailypulse", BRAND_NAME="Daily Pulse", ALERT_WEBHOOK_URL="")
        p.start()
        self.addCleanup(p.stop)


# ------------------------------------------------------------------ dedupe
class TestDedupe(Base):
    def test_canonical_url_strips_tracking(self):
        self.assertEqual(dedupe.canonical_url("https://www.X.com/a/?utm_source=t&id=2#f"), "https://x.com/a?id=2")

    def test_batch_and_history(self):
        a, b = make_article(1), make_article(1)
        b["url"] = a["url"] + "?utm_campaign=z"
        self.assertEqual(len(dedupe.dedupe_batch([a, b])), 1)
        self.assertEqual(dedupe.filter_published([a], [{"url": a["url"], "title": "x"}]), [])

    def test_cluster_same_story_across_sources(self):
        t1 = {"title": "India beat Australia in thrilling World Cup final at Ahmedabad", "summary": "", "url": "u1"}
        t2 = {"title": "Thrilling World Cup final: India beat Australia at Ahmedabad", "summary": "", "url": "u2"}
        t3 = {"title": "Quarterly inflation figures released by central bank today", "summary": "", "url": "u3"}
        groups = dedupe.cluster_articles([t1, t2, t3])
        self.assertEqual(sorted(len(g) for g in groups), [1, 2])

    def test_similar_to_history(self):
        c = make_cluster()
        self.assertTrue(dedupe.similar_to_history(c, ["Apple unveils new chip for thin laptops, longer battery"]))
        self.assertFalse(dedupe.similar_to_history(c, ["Cricket final ends in dramatic tie"]))


# ------------------------------------------------------------------ ranker
class TestRanker(Base):
    def test_freshness_decays(self):
        self.assertGreater(ranker.freshness_score(0), ranker.freshness_score(6))
        self.assertAlmostEqual(ranker.freshness_score(6), 50.0, places=1)

    def test_category_refined_when_feed_label_is_wrong(self):
        """Seen in a real post: a music story from a tech feed was labelled TECHNOLOGY."""
        a = make_article(1, "Technology", "The Verge")
        a["title"] = "Trent Reznor's production work on Prick's debut album revealed"
        a["summary"] = ("While recording the record, the musician contributed to his friend's band project. "
                        "The album credits him on four songs.")
        self.assertEqual(ranker.build_clusters([a])[0]["category"], "Entertainment")
        t = make_article(2, "Technology", "Wired")
        t["title"] = "OpenAI chip startup unveils new processor for AI developers"
        self.assertEqual(ranker.build_clusters([t])[0]["category"], "Technology")
        self.assertEqual(ranker.refine_category("News", "Council approves new budget plan", "Details of the plan."), "News")

    def test_sensitive_skipped(self):
        a = make_article(1)
        a["summary"] += " The murder case shocked the town."
        cl = ranker.build_clusters([a])
        self.assertTrue(cl[0]["sensitive"])
        self.assertIsNone(ranker.pick_next(cl, [], config.CATEGORY_WEIGHTS, min_score=0))

    def test_category_balance_regression(self):
        """Old bug: News dominated. Even when News scores higher, picks must follow the weights."""
        arts = []
        for i in range(40):
            arts.append(make_article(i, "News", "BBC News"))
            arts.append(make_article(100 + i, "Technology", "Wired"))
            arts.append(make_article(200 + i, "Sports", "TOI Sports"))
            arts.append(make_article(300 + i, "Entertainment", "Times of India Entertainment"))
        clusters = ranker.build_clusters(arts)
        weights = {"News": 3, "Technology": 3, "Sports": 2, "Entertainment": 2}
        recent, counts = [], {k: 0 for k in weights}
        for _ in range(20):
            c = ranker.pick_next(clusters, recent[-10:], weights, min_score=0)
            clusters = [x for x in clusters if x is not c]
            recent.append(c["category"])
            counts[c["category"]] += 1
        self.assertTrue(all(3 <= v <= 7 for v in counts.values()), counts)
        self.assertGreaterEqual(counts["Technology"], counts["Sports"] - 1)

    def test_breaking_overrides_balance(self):
        now = datetime.now(timezone.utc)
        arts = [make_article(i, "Sports", s, age_min=5, now=now) for i, s in enumerate(["BBC Sport", "ESPN Cricinfo", "NDTV Sports"])]
        for a in arts:  # same story in three sources
            a["title"] = "Massive record final: champions crowned after historic tournament decider"
            a["summary"] = "x " * 90
        cl = ranker.build_clusters(arts, now)
        with mock.patch.object(config, "BREAKING_SCORE", 60.0):
            cl = ranker.build_clusters(arts, now)
            self.assertTrue(cl[0]["breaking"])


# ------------------------------------------------------------------ AI writer
class TestAI(Base):
    def writer(self, fn):
        return AIWriter(generate_json=fn, cache_path=self.tmp / "ai.json", sleep=lambda s: None)

    def test_valid_output_and_hashtags(self):
        content, mode = self.writer(lambda p: json.dumps(VALID)).write(make_cluster())
        self.assertEqual(mode, "ai")
        self.assertLessEqual(len(content["hashtags"]), 12)
        self.assertTrue(all(re.fullmatch(r"#[a-z0-9_]{3,30}", h) for h in content["hashtags"]))
        self.assertEqual(len(set(content["hashtags"])), len(content["hashtags"]))

    def test_code_fence_json_is_parsed(self):
        content, mode = self.writer(lambda p: "```json\n" + json.dumps(VALID) + "\n```").write(make_cluster())
        self.assertEqual(mode, "ai")

    def test_generic_filler_rejected_then_fallback(self):
        bad = dict(VALID, why_it_matters="Highlights key updates on this developing story for everyone.")
        content, mode = self.writer(lambda p: json.dumps(bad)).write(make_cluster())
        self.assertEqual(mode, "fallback")
        blob = json.dumps(content)
        self.assertNotIn("key updates", blob.lower())
        self.assertNotIn("developing story", blob.lower())

    def test_invented_numbers_rejected(self):
        bad = dict(VALID, hook="Apple chip is 40% faster than rivals today")
        with self.assertRaises(ContentError):
            ai_writer.validate_content(bad, "Apple announced a new processor.", "Technology")
        ok = dict(VALID, hook="Apple chip is 40% faster than rivals today")
        ai_writer.validate_content(ok, "Apple says the chip is 40% faster.", "Technology")

    def test_retry_then_success(self):
        calls = []

        def fn(prompt):
            calls.append(prompt)
            return "not json" if len(calls) == 1 else json.dumps(VALID)
        _, mode = self.writer(fn).write(make_cluster())
        self.assertEqual((mode, len(calls)), ("ai", 2))
        self.assertIn("rejected", calls[1])

    def test_cache_prevents_second_call(self):
        n = []
        w = self.writer(lambda p: (n.append(1), json.dumps(VALID))[1])
        w.write(make_cluster())
        _, mode = w.write(make_cluster())
        self.assertEqual((mode, len(n)), ("cache", 1))

    def test_fallback_has_no_placeholder_and_no_why(self):
        content = ai_writer.fallback_content(make_cluster())
        self.assertIsNone(content["why_it_matters"])
        self.assertGreaterEqual(len(content["slides"]), 1)
        self.assertNotIn("Highlights key updates", json.dumps(content))
        cap = ai_writer.compose_caption(content, make_cluster())
        self.assertNotIn("Why it matters", cap)

    def test_caption_limits(self):
        content = dict(ai_writer.validate_content(VALID, "", "Technology"))
        content["caption"] = "word " * 400
        content["hashtags"] = ["#tag%03d" % i for i in range(12)]
        cap = ai_writer.compose_caption(content, make_cluster())
        self.assertLessEqual(len(cap), 2200)
        self.assertIn("Source: TechCrunch, The Verge", cap)

    def test_no_api_key_means_fallback_without_crash(self):
        w = AIWriter(api_key="", cache_path=self.tmp / "c.json")
        self.assertFalse(w.ai_enabled)
        self.assertEqual(w.write(make_cluster())[1], "fallback")

    def test_caption_keeps_paragraph_breaks(self):
        content = ai_writer.validate_content(VALID, "", "Technology")
        self.assertIn("\n\n", content["caption"])

    def test_pick_flash_model_prefers_newest_stable_general_model(self):
        names = ["models/gemini-2.5-flash", "models/gemini-3.5-flash", "models/gemini-3.6-flash-preview",
                 "models/gemini-3.1-flash-lite", "models/gemini-3.1-flash-image", "models/gemini-3.5-flash-live",
                 "models/gemini-2.5-pro", "models/embedding-001"]
        self.assertEqual(ai_writer.pick_flash_model(names), "gemini-3.5-flash")
        self.assertIsNone(ai_writer.pick_flash_model(["models/gemini-2.5-pro"]))

    def test_404_model_triggers_discovery_once(self):
        from google.genai import errors

        class Model:
            def __init__(self, name): self.name, self.supported_actions = name, ["generateContent"]

        class Models:
            def __init__(self): self.used = []
            def generate_content(self, model, contents, config):
                self.used.append(model)
                if model == "gemini-2.5-flash":
                    raise errors.ClientError(404, {"error": {"message": "not found", "status": "NOT_FOUND"}})
                return mock.Mock(text='{"ok": true}')
            def list(self): return [Model("models/gemini-3.5-flash"), Model("models/gemini-2.5-flash")]

        w = AIWriter(api_key="k" * 20, model="gemini-2.5-flash", cache_path=self.tmp / "m.json")
        w._client = mock.Mock(models=Models())
        self.assertIn("ok", w._generate("hi"))
        self.assertEqual(w.model, "gemini-3.5-flash")
        self.assertEqual(w._client.models.used, ["gemini-2.5-flash", "gemini-3.5-flash"])

    def _fake_gemini(self, behaviour):
        from google.genai import errors

        class Model:
            def __init__(self, name): self.name, self.supported_actions = name, ["generateContent"]

        class Models:
            def __init__(self): self.used = []
            def generate_content(self, model, contents, config):
                self.used.append(model)
                b = behaviour.get(model, "ok")
                if b == "503":
                    raise errors.ServerError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
                if b == "404":
                    raise errors.ClientError(404, {"error": {"message": "nf", "status": "NOT_FOUND"}})
                return mock.Mock(text='{"ok": true}')
            def list(self): return [Model("models/gemini-3.8-flash"), Model("models/gemini-3.5-flash")]
        w = AIWriter(api_key="k" * 20, model="gemini-3.8-flash", cache_path=self.tmp / "g.json", sleep=lambda s: None)
        w._client = mock.Mock(models=Models())
        return w

    def test_503_is_retried_on_same_model(self):
        from google.genai import errors
        w = self._fake_gemini({})
        real, calls = w._client.models.generate_content, {"n": 0}

        def flaky(model, contents, config):
            calls["n"] += 1
            if calls["n"] < 3:  # two overloads, then success
                raise errors.ServerError(503, {"error": {"message": "overloaded"}})
            return real(model, contents, config)
        w._client.models.generate_content = flaky
        self.assertIn("ok", w._generate("hi"))
        self.assertEqual((w.model, calls["n"]), ("gemini-3.8-flash", 3))

    def test_persistent_503_switches_to_other_model(self):
        w = self._fake_gemini({"gemini-3.8-flash": "503"})
        self.assertIn("ok", w._generate("hi"))
        self.assertEqual(w.model, "gemini-3.5-flash")

    def test_trim(self):
        self.assertLessEqual(len(ai_writer.trim("a " * 300, 50)), 51)
        self.assertEqual(ai_writer.trim("Short.", 50), "Short.")


# ------------------------------------------------------------------ renderer
class TestRenderer(Base):
    def test_renders_valid_instagram_jpegs(self):
        content = ai_writer.validate_content(VALID, "", "Technology")
        paths = image_renderer.render_post(content, make_cluster(), self.tmp, "t")
        self.assertEqual(len(paths), 5)
        for p in paths:
            with Image.open(p) as im:
                self.assertEqual((im.format, im.size, im.mode), ("JPEG", (1080, 1350), "RGB"))
            self.assertLess(p.stat().st_size, 8_000_000)

    def test_emoji_removed_and_long_text_fits(self):
        self.assertEqual(image_renderer.clean_for_image("Hello 🚀 world ✅"), "Hello world")
        img = Image.new("RGB", (1080, 1350))
        d = image_renderer.ImageDraw.Draw(img)
        font, lines, lh = image_renderer.fit_text(d, "word " * 400, 888, 300, start=60, min_size=30)
        self.assertLessEqual(len(lines) * lh, 300)
        self.assertTrue(lines[-1].endswith("…"))

    def test_unbreakable_token_is_wrapped(self):
        img = Image.new("RGB", (1080, 1350))
        d = image_renderer.ImageDraw.Draw(img)
        lines = image_renderer.wrap(d, "x" * 300, image_renderer.load_font(50), 888)
        self.assertTrue(all(d.textlength(l, font=image_renderer.load_font(50)) <= 888 for l in lines))

    def test_fallback_content_without_why_renders(self):
        content = ai_writer.fallback_content(make_cluster())
        paths = image_renderer.render_post(content, make_cluster(), self.tmp, "f")
        self.assertGreaterEqual(len(paths), 3)


# ------------------------------------------------------------------ Instagram client
class TestInstagram(Base):
    def test_carousel_flow_and_no_token_in_url(self):
        s = FakeIG()
        res = make_ig(s).publish_images([f"https://h/{i}.jpg" for i in range(5)], "caption")
        posts = [c for c in s.calls if c[0] == "POST"]
        self.assertEqual([p[1] for p in posts], ["U/media"] * 6 + ["U/media_publish"])
        self.assertTrue(all(p[2].get("is_carousel_item") == "true" for p in posts[:5]))
        self.assertEqual(posts[5][2]["media_type"], "CAROUSEL")
        self.assertEqual(posts[5][2]["children"], "c1,c2,c3,c4,c5")
        self.assertEqual(posts[6][2]["creation_id"], "parent")
        self.assertEqual((res.media_id, res.permalink), ("m1", "https://instagram.com/p/abc/"))

    def test_single_image_path(self):
        s = FakeIG()
        make_ig(s).publish_images(["https://h/1.jpg"], "c")
        self.assertEqual([c[1] for c in s.calls if c[0] == "POST"], ["U/media", "U/media_publish"])

    def test_transient_read_error_is_retried(self):
        s = FakeIG()
        s.overrides["GET:U"] = [Resp(500, {"error": {"message": "oops", "code": 2, "is_transient": True}})]
        self.assertEqual(make_ig(s).verify_account(), "dailypulse")

    def test_token_error_not_retried_and_flagged(self):
        s = FakeIG()
        s.overrides["POST:U/media"] = [Resp(400, {"error": {"message": "expired", "code": 190}})]
        with self.assertRaises(InstagramError) as cm:
            make_ig(s).publish_images(["https://h/1.jpg"], "c")
        self.assertTrue(cm.exception.token_problem)
        self.assertEqual(len([c for c in s.calls if c[0] == "POST"]), 1)

    def test_container_error_status_raises(self):
        s = FakeIG()
        s.status["c1"] = "ERROR"
        with self.assertRaises(InstagramError):
            make_ig(s).publish_images(["https://h/1.jpg", "https://h/2.jpg"], "c")
        self.assertEqual(s.published, 0)

    def test_poll_timeout(self):
        s = FakeIG()
        s.status["single"] = "IN_PROGRESS"
        t = iter(range(0, 10_000, 40))
        ig = make_ig(s, poll_timeout=100, clock=lambda: next(t))
        with self.assertRaises(InstagramError) as cm:
            ig.publish_images(["https://h/1.jpg"], "c")
        self.assertIn("not ready", str(cm.exception))

    def test_ambiguous_publish_is_recovered_not_duplicated(self):
        s = FakeIG()
        s.overrides["POST:U/media_publish"] = [requests.Timeout()]
        ig = make_ig(s)
        s.overrides["GET:U/media"] = [Resp(200, {"data": [{"id": "m9", "caption": "My caption", "permalink": "p"}]})]
        res = ig.publish_images(["https://h/1.jpg", "https://h/2.jpg"], "My caption")
        self.assertEqual(res.media_id, "m9")
        self.assertEqual(s.published, 0)  # publish was never repeated

    def test_write_timeout_never_blindly_retried(self):
        s = FakeIG()
        s.overrides["POST:U/media"] = [requests.Timeout()]
        with self.assertRaises(InstagramError):
            make_ig(s).publish_images(["https://h/1.jpg"], "c")
        self.assertEqual(len([c for c in s.calls if c[0] == "POST"]), 1)

    def test_usage_and_insights(self):
        ig = make_ig()
        self.assertEqual(ig.publishing_usage(), (1, 100))
        self.assertEqual(ig.insights("m1"), {"reach": 500})

    def test_cloudinary_error_reason_is_reported(self):
        class S:
            def post(self, url, data=None, files=None, timeout=None):
                return Resp(401, {"error": {"message": "Invalid api_key abc"}})
        p = self.tmp / "z.jpg"
        Image.new("RGB", (5, 5)).save(p, "JPEG")
        with self.assertRaises(media_host.MediaHostError) as cm:
            media_host.CloudinaryHost("c", "k", "s", session=S()).upload(p, "z.jpg")
        self.assertIn("Invalid api_key", str(cm.exception))

    def test_carousel_size_validation(self):
        with self.assertRaises(InstagramError):
            make_ig().create_carousel(["https://h/1.jpg"] * 11, "c")


# ------------------------------------------------------------------ schedule / storage / health
class TestSchedule(Base):
    T = ["09:00", "13:00", "23:30"]

    def test_slots(self):
        d = datetime(2026, 10, 5, 9, 30, tzinfo=IST)
        self.assertEqual(schedule.current_slot(d, self.T, 90, set()), "2026-10-05 09:00")
        self.assertIsNone(schedule.current_slot(d, self.T, 90, {"2026-10-05 09:00"}))
        self.assertIsNone(schedule.current_slot(datetime(2026, 10, 5, 10, 31, tzinfo=IST), self.T, 90, set()))

    def test_window_crossing_midnight(self):
        d = datetime(2026, 10, 6, 0, 20, tzinfo=IST)
        self.assertEqual(schedule.current_slot(d, self.T, 90, set()), "2026-10-05 23:30")


class TestStorage(Base):
    def test_atomic_write_leaves_no_temp_files(self):
        p = self.tmp / "x.json"
        storage.write_json(p, {"a": 1})
        storage.write_json(p, {"a": 2})
        self.assertEqual(storage.read_json(p, None), {"a": 2})
        self.assertEqual([f.name for f in self.tmp.iterdir() if f.suffix == ".tmp"], [])

    def test_corrupt_file_returns_default(self):
        p = self.tmp / "bad.json"
        p.write_text("{not json")
        self.assertEqual(storage.read_json(p, []), [])

    def test_lock_blocks_then_heals_when_stale(self):
        lock = storage.ProcessLock(self.tmp / "l.lock", ttl_minutes=30)
        with lock:
            with self.assertRaises(storage.LockError):
                storage.ProcessLock(self.tmp / "l.lock").acquire()
        (self.tmp / "l.lock").write_text("1")
        old = time.time() - 3600
        os.utime(self.tmp / "l.lock", (old, old))
        storage.ProcessLock(self.tmp / "l.lock").acquire()  # stale -> healed

    def test_retention_keeps_state_small(self):
        """Old bug: 19 MB trend cache + 5 MB lifecycle committed to git."""
        now = datetime.now(timezone.utc)
        recs = [{"published_at": (now - timedelta(hours=i)).isoformat(), "x": "y" * 50} for i in range(5000)]
        pruned = storage.prune_records(recs, max_records=400, max_days=60)
        self.assertLessEqual(len(pruned), 400)
        self.assertLess(len(json.dumps(pruned)), 100_000)
        cache = {str(i): {"ts": time.time() - i} for i in range(1000)}
        self.assertLessEqual(len(storage.prune_dict_by_age(cache, max_entries=150, max_age_hours=24)), 150)


class TestHealth(Base):
    def test_breaker_and_recovery(self):
        t = [datetime(2026, 10, 5, tzinfo=timezone.utc)]
        h = health.Health(self.tmp / "h.json", now_fn=lambda: t[0])
        with mock.patch.object(config, "BREAKER_FAILURES", 3):
            for _ in range(3):
                h.record_failure("boom")
            self.assertIsNotNone(h.paused_until())
            t[0] += timedelta(hours=4)
            self.assertIsNone(h.paused_until())
        h.record_success()
        self.assertEqual(h.state["consecutive_failures"], 0)

    def test_secret_masking(self):
        with mock.patch.object(config, "IG_ACCESS_TOKEN", "EAAB1234567890SECRETTOKEN"):
            out = health.mask_secrets("failed https://x?access_token=EAAB1234567890SECRETTOKEN&a=1 key EAAB1234567890SECRETTOKEN")
            self.assertNotIn("SECRETTOKEN", out)
        self.assertNotIn("AIzaSyA1234567890123456789012", health.mask_secrets("k=AIzaSyA1234567890123456789012"))

    def test_alert_is_throttled(self):
        posts = []

        class S:
            def post(self, url, json=None, timeout=None):
                posts.append(json)
        h = health.Health(self.tmp / "h2.json", session=S())
        with mock.patch.object(config, "ALERT_WEBHOOK_URL", "https://hook.test/x"):
            self.assertTrue(h.alert("first"))
            self.assertFalse(h.alert("second"))
            self.assertTrue(h.alert("forced", force=True))
        self.assertEqual(len(posts), 2)


# ------------------------------------------------------------------ media hosts
class TestMedia(Base):
    def jpg(self):
        p = self.tmp / "a.jpg"
        Image.new("RGB", (10, 10)).save(p, "JPEG")
        return p

    def test_cloudinary_signature_and_jpg_url(self):
        posted = {}

        class S:
            def post(self, url, data=None, files=None, timeout=None):
                posted.update(url=url, data=data)
                return Resp(200, {"secure_url": "https://res.cloudinary.com/c/image/upload/v1/insta_agent/a.png"})
        h = media_host.CloudinaryHost("cloud", "key", "sekret", session=S()).upload(self.jpg(), "a.jpg")
        d = posted["data"]
        expect = hashlib.sha1(f"public_id={d['public_id']}&timestamp={d['timestamp']}sekret".encode()).hexdigest()
        self.assertEqual(d["signature"], expect)
        self.assertTrue(h.url.endswith(".jpg"))

    def test_github_host_creates_branch_and_returns_raw_url(self):
        calls = []

        class S:
            def get(self, url, headers=None, params=None, timeout=None):
                calls.append(("GET", url))
                if url.endswith("heads/media"):
                    return Resp(404)
                if url.endswith("/repos/o/r"):
                    return Resp(200, {"private": False, "default_branch": "main"})
                return Resp(200, {"object": {"sha": "abc"}})

            def post(self, url, headers=None, json=None, timeout=None):
                calls.append(("POST", url, json))
                return Resp(201)

            def put(self, url, headers=None, json=None, timeout=None):
                calls.append(("PUT", url, json["branch"]))
                return Resp(201, {"content": {"sha": "s1"}})

            def delete(self, url, headers=None, json=None, timeout=None):
                calls.append(("DELETE", url, json["sha"]))
                return Resp(200)
        gh = media_host.GitHubHost("tok", "o/r", "media", session=S())
        h = gh.upload(self.jpg(), "20261005_x_01.jpg")
        self.assertEqual(h.url, "https://raw.githubusercontent.com/o/r/media/media/20261005_x_01.jpg")
        self.assertIn(("POST", "https://api.github.com/repos/o/r/git/refs", {"ref": "refs/heads/media", "sha": "abc"}), calls)
        gh.delete(h.ref)
        self.assertEqual(calls[-1][2], "s1")

    def test_github_host_rejects_private_repo(self):
        class S:
            def get(self, url, headers=None, params=None, timeout=None):
                return Resp(404) if url.endswith("heads/media") else Resp(200, {"private": True, "default_branch": "main"})
        with self.assertRaises(media_host.MediaHostError):
            media_host.GitHubHost("t", "o/r", session=S()).upload(self.jpg(), "a.jpg")

    def test_wait_until_public_checks_content_type(self):
        class Good:
            def get(self, url, **kw):
                return Resp(200, headers={"Content-Type": "image/jpeg"})

        class Bad:
            def get(self, url, **kw):
                return Resp(200, headers={"Content-Type": "text/html"})
        self.assertTrue(media_host.wait_until_public("https://x/a.jpg", session=Good(), sleep=lambda s: None))
        self.assertFalse(media_host.wait_until_public("https://x/a.jpg", attempts=2, session=Bad(), sleep=lambda s: None))


# ------------------------------------------------------------------ end-to-end pipeline
class PipelineFixture(Base):
    """Shared fakes for pipeline tests (no tests of its own)."""
    NOW = datetime(2026, 10, 5, 3, 40, tzinfo=timezone.utc)  # 09:10 IST -> slot 09:00 due

    def build(self, articles=None, ig=None, now_fn=None, ai=lambda p: json.dumps(VALID)):
        arts = articles if articles is not None else [make_article(i, c, s, now=self.NOW) for i, (c, s) in enumerate(
            [("News", "BBC News"), ("Technology", "Wired"), ("Sports", "BBC Sport"), ("Entertainment", "Variety")])]
        self.ig_session = ig or FakeIG()
        self.host = media_host.LocalHost("https://cdn.test/media", self.tmp / "public")
        return Pipeline(collector=lambda: arts, writer=AIWriter(generate_json=ai, cache_path=self.tmp / "ai.json", sleep=lambda s: None),
                        host=self.host, ig=make_ig(self.ig_session), now_fn=now_fn or (lambda: self.NOW),
                        data_dir=self.tmp, sleep=lambda s: None, verify_public=lambda u: True)


class TestPipeline(PipelineFixture):
    def test_happy_path_publishes_records_and_cleans_up(self):
        p = self.build()
        res = p.run_cycle()
        self.assertEqual(res["status"], "published", res)
        hist = p.load_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["media_id"], "m1")
        self.assertIn("2026-10-05 09:00", storage.read_json(self.tmp / "schedule_state.json", {})["done"])
        self.assertEqual(list((self.tmp / "public").glob("*.jpg")) if (self.tmp / "public").exists() else [], [])
        self.assertEqual(p.health.state["status"], "ONLINE")
        caps = [c[2]["caption"] for c in self.ig_session.calls if c[0] == "POST" and c[2].get("caption")]
        self.assertLessEqual(len(caps[0]), 2200)
        self.assertNotIn("TOKEN_SECRET", json.dumps(self.ig_session.calls[0][1]))

    def test_second_run_in_same_slot_does_not_double_post(self):
        p = self.build()
        p.run_cycle()
        res = self.build().run_cycle()
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(self.ig_session.published, 0)

    def test_outside_slot_is_idle(self):
        late = self.NOW + timedelta(hours=3)  # 12:10 IST -> no slot
        res = self.build(now_fn=lambda: late, articles=[]).run_cycle()
        self.assertEqual(res["status"], "idle")

    def test_daily_cap(self):
        with mock.patch.object(config, "MAX_POSTS_PER_DAY", 1):
            self.build().run_cycle()
            later = self.NOW + timedelta(hours=4)
            res = self.build(now_fn=lambda: later).run_cycle(force=False)
            self.assertIn("cap", res.get("reason", ""))

    def test_publish_failure_is_recorded_and_never_loops(self):
        s = FakeIG()
        s.overrides["POST:U/media_publish"] = [Resp(400, {"error": {"message": "bad", "code": 100}})] * 5
        p = self.build(ig=s)
        res = p.run_cycle()
        self.assertEqual(res["status"], "failed")
        self.assertEqual(p.health.state["consecutive_failures"], 1)
        self.assertEqual(p.load_history(), [])
        self.assertLessEqual(s.published, 0)
        att = storage.read_json(self.tmp / "attempts.json", {})
        self.assertTrue(all(v["count"] <= 2 for v in att.values()))

    def test_token_expiry_fails_fast_with_clear_error(self):
        s = FakeIG()
        s.overrides["GET:U/content_publishing_limit"] = [Resp(400, {"error": {"message": "Session has expired", "code": 190}})]
        res = self.build(ig=s).run_cycle()
        self.assertEqual(res["status"], "failed")
        self.assertIn("token", res["error"].lower())

    def test_all_feeds_down_is_reported_as_failure(self):
        from core.collector import CollectionError

        def boom():
            raise CollectionError("all 15 feeds failed")
        p = self.build()
        p.collector = boom
        self.assertEqual(p.run_cycle()["status"], "failed")

    def test_dry_run_publishes_nothing(self):
        p = self.build()
        res = p.run_cycle(dry_run=True)
        self.assertEqual(res["status"], "dry_run")
        self.assertTrue(all(Path(f).exists() for f in res["files"]))
        self.assertEqual(self.ig_session.published, 0)
        self.assertEqual(p.load_history(), [])
        self.assertFalse((self.tmp / "health_state.json").exists())

    def test_paused_circuit_breaker_blocks_run(self):
        p = self.build()
        p.health.state["paused_until"] = (self.NOW + timedelta(hours=1)).isoformat()
        self.assertEqual(p.run_cycle()["status"], "skipped")

    def test_lock_prevents_parallel_runs(self):
        (self.tmp / "agent.lock").write_text("1")
        self.assertEqual(self.build().run_cycle()["reason"], "locked")

    def test_already_posted_story_is_not_reposted(self):
        arts = [make_article(1, "News", "BBC News", now=self.NOW)]
        self.build(articles=arts).run_cycle()
        later = self.NOW + timedelta(hours=5)  # 14:10 IST, slot 13:00 is within grace
        res = self.build(articles=arts, now_fn=lambda: later).run_cycle()
        self.assertEqual(res["status"], "idle")

    def test_balanced_over_many_posts(self):
        arts = []
        for i in range(30):
            for c, s in [("News", "BBC News"), ("Technology", "Wired"), ("Sports", "BBC Sport"), ("Entertainment", "Variety")]:
                arts.append(make_article(i + {"News": 0, "Technology": 100, "Sports": 200, "Entertainment": 300}[c], c, s, now=self.NOW))
        t = [self.NOW]
        with mock.patch.object(config, "MIN_POST_INTERVAL_MINUTES", 0):
            p = self.build(articles=arts, now_fn=lambda: t[0], ai=lambda x: json.dumps(VALID))
            for _ in range(10):
                self.assertEqual(p.run_cycle(force=True)["status"], "published")
                t[0] += timedelta(hours=1)
        cats = [h["category"] for h in p.load_history()]
        for c in ("News", "Technology", "Sports", "Entertainment"):
            self.assertGreaterEqual(cats.count(c), 2, cats)


# ------------------------------------------------------------------ video / reels
class TestReels(Base):
    def slides(self):
        content = ai_writer.validate_content(VALID, "", "Technology")
        return image_renderer.render_slides(content, make_cluster(), layout=image_renderer.REEL)

    def test_reel_layout_is_9x16_and_text_inside_safe_zone(self):
        for im in self.slides():
            self.assertEqual(im.size, (1080, 1920))
        L = image_renderer.REEL
        self.assertGreaterEqual(L.content_top, 380)       # clear of the top UI
        self.assertLessEqual(L.content_bottom, 1920 - 450)  # clear of caption/buttons
        self.assertLess(L.text_w, 1080 - 2 * image_renderer.MARGIN + 1)

    def test_encoded_video_matches_instagram_specs(self):
        out = video_renderer.encode_slideshow(self.slides(), self.tmp / "r.mp4", seconds_per_slide=0.6, fps=10)
        info = video_renderer.probe(out)
        self.assertEqual((info["video"], info["audio"], info["size"]), ("h264", "aac", (1080, 1920)))
        self.assertGreaterEqual(info["duration"], 4.5)      # never below Instagram's minimum, even with tiny settings
        self.assertLess(info["duration"], 90)
        self.assertLess(out.stat().st_size, 100_000_000)

    def test_encoder_errors_are_clear(self):
        with self.assertRaises(video_renderer.VideoError):
            video_renderer.encode_slideshow(self.slides()[:1], self.tmp / "a.mp4")
        with mock.patch.object(video_renderer, "find_ffmpeg", return_value=None):
            with self.assertRaises(video_renderer.VideoError) as cm:
                video_renderer.encode_slideshow(self.slides(), self.tmp / "b.mp4")
            self.assertIn("imageio-ffmpeg", str(cm.exception))
        with self.assertRaises(video_renderer.VideoError):  # would exceed the 90 s API cap
            video_renderer.encode_slideshow(self.slides(), self.tmp / "c.mp4", seconds_per_slide=30)

    def test_missing_music_file_falls_back_to_silence(self):
        out = video_renderer.encode_slideshow(self.slides(), self.tmp / "m.mp4", seconds_per_slide=0.6, fps=10,
                                              music_path=str(self.tmp / "nope.mp3"))
        self.assertEqual(video_renderer.probe(out)["audio"], "aac")

    def test_instagram_reel_flow(self):
        s = FakeIG()
        res = make_ig(s).publish_reel("https://cdn.test/v.mp4", "cap", share_to_feed=True)
        posts = [c for c in s.calls if c[0] == "POST"]
        self.assertEqual([p[1] for p in posts], ["U/media", "U/media_publish"])
        self.assertEqual(posts[0][2]["media_type"], "REELS")
        self.assertEqual(posts[0][2]["video_url"], "https://cdn.test/v.mp4")
        self.assertEqual(posts[0][2]["share_to_feed"], "true")
        self.assertEqual(posts[1][2]["creation_id"], "reel1")
        self.assertEqual(res.media_id, "m1")

    def test_reel_processing_error_shows_instagram_reason(self):
        s = FakeIG()
        s.overrides["GET:reel1"] = [Resp(200, {"status_code": "ERROR", "status": "Error: video codec not supported"})]
        with self.assertRaises(InstagramError) as cm:
            make_ig(s).publish_reel("https://cdn.test/v.mp4", "cap")
        self.assertIn("codec not supported", str(cm.exception))
        self.assertEqual(s.published, 0)

    def test_reel_ambiguous_publish_recovered(self):
        s = FakeIG()
        s.overrides["POST:U/media_publish"] = [requests.Timeout()]
        s.overrides["GET:U/media"] = [Resp(200, {"data": [{"id": "m7", "caption": "Reel caption", "permalink": "p"}]})]
        self.assertEqual(make_ig(s).publish_reel("https://cdn.test/v.mp4", "Reel caption").media_id, "m7")
        self.assertEqual(s.published, 0)

    def test_pick_format_honours_weights_and_alternates(self):
        seq, recent = [], []
        for _ in range(6):
            f = ranker.pick_format(recent, {"carousel": 1, "reel": 1})
            seq.append(f)
            recent.append(f)
        self.assertEqual(seq, ["carousel", "reel"] * 3)
        recent, counts = [], {"carousel": 0, "reel": 0}
        for _ in range(12):
            f = ranker.pick_format(recent, {"carousel": 2, "reel": 1})
            recent.append(f)
            counts[f] += 1
        self.assertEqual(counts, {"carousel": 8, "reel": 4})
        self.assertEqual(ranker.pick_format([], {"reel": 1}), "reel")

    def test_cloudinary_video_upload_and_delete(self):
        calls = []

        class S:
            def post(self, url, data=None, files=None, timeout=None):
                calls.append(url)
                return Resp(200, {"secure_url": "https://res.cloudinary.com/c/video/upload/v1/insta_agent/v.mp4"})
        host = media_host.CloudinaryHost("c", "k", "s", session=S())
        v = self.tmp / "v.mp4"
        v.write_bytes(b"0" * 100)
        h = host.upload(v, "v.mp4")
        self.assertTrue(calls[0].endswith("/video/upload"))
        self.assertTrue(h.url.endswith(".mp4"))
        host.delete(h.ref)
        self.assertTrue(calls[1].endswith("/video/destroy"))

    def test_wait_until_public_video(self):
        def sess(ctype):
            class S:
                def get(self, url, **kw):
                    return Resp(200, headers={"Content-Type": ctype})
            return S()
        ok = lambda c: media_host.wait_until_public("https://x/v.mp4", kind="video", attempts=1, session=sess(c))  # noqa: E731
        self.assertTrue(ok("video/mp4"))
        self.assertTrue(ok("application/octet-stream"))
        self.assertFalse(ok("text/html"))

    def test_config_rejects_unknown_format(self):
        with mock.patch.object(config, "CONTENT_FORMATS", {"story": 1}):
            self.assertTrue(any("CONTENT_FORMATS" in p for p in config.validate("dry")))


class TestReelPipeline(PipelineFixture):
    """Same pipeline fixtures, but publishing video."""

    def run_reel(self, **kw):
        kinds = []
        p = self.build(**kw)
        p.verify_public = lambda url, kind="image": (kinds.append(kind), True)[1]
        with mock.patch.object(config, "CONTENT_FORMATS", {"reel": 1}):
            res = p.run_cycle()
        return p, res, kinds

    def test_reel_published_and_cleaned_up(self):
        p, res, kinds = self.run_reel()
        self.assertEqual((res["status"], res["format"]), ("published", "reel"), res)
        self.assertEqual(kinds, ["video"])
        create = next(c for c in self.ig_session.calls if c[0] == "POST" and c[2].get("media_type") == "REELS")
        self.assertTrue(create[2]["video_url"].endswith(".mp4"))
        self.assertEqual(p.load_history()[0]["format"], "reel")
        left = list((self.tmp / "public").glob("*")) if (self.tmp / "public").exists() else []
        self.assertEqual(left, [])

    def test_video_failure_falls_back_to_carousel(self):
        with mock.patch("core.pipeline.encode_slideshow", side_effect=video_renderer.VideoError("boom")):
            p, res, _ = self.run_reel()
        self.assertEqual((res["status"], res["format"]), ("published", "carousel"), res)
        self.assertEqual(self.ig_session.published, 1)

    def test_no_ffmpeg_disables_reels_instead_of_failing(self):
        with mock.patch("core.pipeline.encoder_available", return_value=False):
            p, res, _ = self.run_reel()
        self.assertEqual((res["status"], res["format"]), ("published", "carousel"), res)

    def test_dry_run_reel_writes_mp4_preview(self):
        p = self.build()
        res = p.run_cycle(dry_run=True, fmt="reel")
        self.assertEqual((res["status"], res["format"]), ("dry_run", "reel"))
        self.assertTrue(any(f.endswith(".mp4") and Path(f).exists() for f in res["files"]))
        self.assertEqual(self.ig_session.published, 0)

    def test_formats_alternate_across_posts(self):
        arts = [make_article(i, "News", "BBC News", now=self.NOW) for i in range(10)]
        t = [self.NOW]
        p = self.build(articles=arts, now_fn=lambda: t[0])
        with mock.patch.multiple(config, CONTENT_FORMATS={"carousel": 1, "reel": 1}, MIN_POST_INTERVAL_MINUTES=0):
            fmts = []
            for _ in range(4):
                fmts.append(p.run_cycle(force=True)["format"])
                t[0] += timedelta(hours=1)
        self.assertEqual(fmts, ["carousel", "reel", "carousel", "reel"])



# ------------------------------------------------------------------ dynamic reels (footage + voice + captions)
class FakeSession:
    """requests-like session for Pexels search + clip download."""

    def __init__(self, videos=None, status=200):
        self.videos, self.status, self.calls = videos or [], status, []

    def get(self, url, headers=None, params=None, timeout=None, stream=False):
        self.calls.append((url, headers, params))
        if "search" in url:
            return Resp(self.status, {"videos": self.videos})

        class Dl(Resp):
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def iter_content(s, n): yield b"0" * 20_000
        return Dl(200)


def pexels_video(i, files, duration=9, slug="stadium-crowd-cheering"):
    return {"id": i, "duration": duration, "url": f"https://www.pexels.com/video/{slug}-{i}/", "video_files": files}


def mp4_file(w, h, link="https://v.test/a.mp4"):
    return {"file_type": "video/mp4", "width": w, "height": h, "link": link}


class TestVoice(Base):
    def test_distribute_words_covers_duration_and_pauses_at_sentences(self):
        words = tts.distribute_words("Hello world. This is a test, right?", 10.0)
        self.assertEqual(len(words), 7)
        self.assertAlmostEqual(words[-1][2], 10.0, places=3)
        self.assertTrue(all(a[2] <= b[1] + 1e-9 for a, b in zip(words, words[1:])))
        dur = lambda w: w[2] - w[1]  # noqa: E731
        self.assertGreater(dur(words[1]), dur(words[0]) * 0.6)  # full stop earns extra time

    def test_boundaries_mapping_requires_matching_word_count(self):
        ok = tts.words_from_boundaries("one two", [(0.0, 0.4, "one"), (0.5, 0.4, "two")])
        self.assertEqual([w[0] for w in ok], ["one", "two"])
        self.assertEqual(tts.words_from_boundaries("one two three", [(0.0, 0.4, "one")]), [])

    def test_edge_provider_uses_word_boundaries(self):
        class Com:
            def __init__(self, text, voice, rate="+0%", boundary="SentenceBoundary"):
                assert boundary == "WordBoundary"  # edge-tts 7 defaults to sentence boundaries
            async def stream(self):
                yield {"type": "audio", "data": b"ID3" + b"0" * 5000}
                yield {"type": "WordBoundary", "offset": 0, "duration": 4_000_000, "text": "Hello"}
                yield {"type": "WordBoundary", "offset": 5_000_000, "duration": 4_000_000, "text": "there"}
                yield {"type": "WordBoundary", "offset": 12_000_000, "duration": 6_000_000, "text": "friends."}
        with mock.patch("edge_tts.Communicate", Com), mock.patch.object(config, "TTS_PROVIDER", "edge"):
            vo = tts.synthesize("Hello there friends.", self.tmp / "v")
        self.assertIsNotNone(vo)
        self.assertEqual(vo.provider, "edge")
        self.assertEqual([w[0] for w in vo.words], ["Hello", "there", "friends."])
        self.assertAlmostEqual(vo.words[2][1], 1.2, places=2)
        self.assertGreater(vo.duration, 1.5)

    def test_failing_providers_never_raise(self):
        class Boom:
            def __init__(self, *a, **k): raise RuntimeError("403 from service")
        with mock.patch("edge_tts.Communicate", Boom), mock.patch.object(config, "GEMINI_API_KEY", ""):
            self.assertIsNone(tts.synthesize("Hello world again.", self.tmp / "v", provider="auto"))
        self.assertIsNone(tts.synthesize("Hello", self.tmp / "v", provider="none"))
        self.assertIsNone(tts.synthesize("   ", self.tmp / "v", provider="edge"))


class TestStock(Base):
    def test_pick_file_prefers_portrait_near_1080_and_skips_4k(self):
        v = pexels_video(1, [mp4_file(2160, 3840, "u4k"), mp4_file(720, 1280, "u720"), mp4_file(1080, 1920, "u1080"),
                             mp4_file(1920, 1080, "landscape"), {"file_type": "video/webm", "width": 1080, "height": 1920, "link": "w"}])
        self.assertEqual(stock.pick_file(v)["link"], "u1080")
        self.assertIsNone(stock.pick_file(pexels_video(2, [mp4_file(1920, 1080)])))

    def test_fetch_clips_downloads_and_sends_key_and_portrait_filter(self):
        sess = FakeSession([pexels_video(1, [mp4_file(1080, 1920)]), pexels_video(2, [mp4_file(1080, 1920)])])
        px = stock.PexelsClient("KEY123", session=sess)
        clips = px.fetch_clips(["stadium crowd", "crowd cheering"], self.tmp / "s", wanted=2)
        self.assertEqual(len(clips), 2)
        self.assertTrue(all(c.exists() and c.stat().st_size > 0 for c in clips))
        url, headers, params = sess.calls[0]
        self.assertEqual(headers["Authorization"], "KEY123")
        self.assertEqual(params["orientation"], "portrait")

    def test_failures_degrade_to_empty_list(self):
        self.assertFalse(stock.PexelsClient("", session=FakeSession()).enabled)
        px = stock.PexelsClient("K", session=FakeSession(status=401))
        self.assertEqual(px.fetch_clips(["a b"], self.tmp / "s2", wanted=2), [])
        short = FakeSession([pexels_video(1, [mp4_file(1080, 1920)], duration=2)])
        self.assertEqual(stock.PexelsClient("K", session=short).fetch_clips(["a"], self.tmp / "s3", wanted=1), [])

    def test_queries_are_cleaned_deduped_and_backed_by_category_defaults(self):
        q = stock.queries_for("Sports", ["Stadium Crowd!!", "stadium crowd", "player 7 scores"])
        self.assertEqual(q[0], "stadium crowd")
        self.assertEqual(q.count("stadium crowd"), 1)
        self.assertIn("player scores", q)
        self.assertIn("athlete running", q)

    def test_local_clips(self):
        d = self.tmp / "mine"
        d.mkdir()
        (d / "a.mp4").write_bytes(b"0")
        (d / "notes.txt").write_text("x")
        self.assertEqual([p.name for p in stock.local_clips(d, 3)], ["a.mp4"])
        self.assertEqual(stock.local_clips("", 3), [])


class TestReelScript(Base):
    SRC = ("Apple announced a new processor on Tuesday for thin laptops. The company says it targets students and "
           "remote workers who need longer battery life. Pricing starts later this year.")

    def test_valid_reel_script(self):
        out = ai_writer.validate_reel(dict(REEL_VALID), self.SRC, "Technology")
        self.assertEqual(len(out["narration"]), 4)
        self.assertEqual(out["broll_queries"], ["laptop typing", "technology circuit board"])
        self.assertTrue(all(h.startswith("#") for h in out["hashtags"]))

    def test_rejections(self):
        def rejected(**kw):
            with self.assertRaises(ContentError):
                ai_writer.validate_reel(dict(REEL_VALID, **kw), self.SRC, "Technology")
        rejected(narration=["Too short.", "Also short here too.", "Tiny sentence only."])               # too few words
        rejected(narration=["word " * 30, "ok sentence number two is here now.", "and a third normal sentence goes here."])
        rejected(narration=REEL_VALID["narration"][:2])                                                  # too few sentences
        rejected(narration=REEL_VALID["narration"] + ["Visit https://example.com for more details today."])
        rejected(narration=REEL_VALID["narration"][:3] + ["The chip is 40% faster than the old model overall."])
        rejected(why_it_matters="Highlights key updates on this developing story for everyone.")

    def test_string_narration_is_split_into_sentences(self):
        out = ai_writer.validate_reel(dict(REEL_VALID, narration=" ".join(REEL_VALID["narration"])), self.SRC, "Technology")
        self.assertEqual(len(out["narration"]), 4)

    def test_fallback_reel_is_usable_without_ai(self):
        fb = ai_writer.fallback_reel(make_cluster())
        self.assertGreaterEqual(len(fb["narration"]), 1)
        self.assertIsNone(fb["why_it_matters"])
        self.assertNotIn("key updates", json.dumps(fb).lower())

    def test_write_reel_uses_reel_prompt_and_separate_cache(self):
        prompts = []

        def fn(prompt):
            prompts.append(prompt)
            return json.dumps(REEL_VALID if "VOICEOVER" in prompt else VALID)
        w = AIWriter(generate_json=fn, cache_path=self.tmp / "c.json", sleep=lambda s: None)
        c = make_cluster()
        _, m1 = w.write_reel(c)
        _, m2 = w.write(c)           # carousel for the same story must not hit the reel cache
        _, m3 = w.write_reel(c)
        self.assertEqual((m1, m2, m3), ("ai", "ai", "cache"))
        self.assertEqual(len(prompts), 2)

    def test_caption_credits_footage(self):
        content = ai_writer.validate_reel(dict(REEL_VALID), self.SRC, "Technology")
        self.assertIn("Footage: Pexels", ai_writer.compose_caption(content, make_cluster(), "Pexels"))
        self.assertNotIn("Footage:", ai_writer.compose_caption(content, make_cluster()))


def make_test_clip(path: Path, seconds=6):
    subprocess.run([video_renderer.find_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc2=s=720x1280:r=30:d={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


class TestReelRenderer(Base):
    def test_chunking_rules(self):
        words = [(w, i * 0.3, i * 0.3 + 0.25) for i, w in enumerate("This is a really long sentence, with commas and more words".split())]
        chunks = reel_renderer.chunk_words(words)
        self.assertTrue(all(len(c) <= 3 for c in chunks))
        self.assertEqual([w for c in chunks for w in c], words)
        self.assertTrue(any(c[-1][0].endswith(",") for c in chunks))  # breaks after punctuation

    def test_caption_images_fit_the_safe_width(self):
        for ws in (["Short", "words"], ["Extraordinarily", "unbelievable", "announcement"], ["Hi"]):
            for im in reel_renderer._render_chunk(ws, (255, 200, 0)):
                self.assertLessEqual(im.width, reel_renderer.CAPTION_MAX_W + 2 * 22 + 4)

    def test_caption_chunks_timeline_is_monotonic(self):
        words = tts.distribute_words("One two three four five six seven eight nine ten.", 6.0, start=0.35)
        chunks = reel_renderer.build_caption_chunks(words, "News", 9.0)
        self.assertTrue(all(c.end > c.start for c in chunks))
        self.assertTrue(all(a.start < b.start for a, b in zip(chunks, chunks[1:])))
        self.assertTrue(all(len(c.states) == len(c.word_starts) for c in chunks))

    def test_full_reel_with_footage_and_voice(self):
        clips = []
        for i in range(2):
            clips.append(self.tmp / f"c{i}.mp4")
            make_test_clip(clips[-1])
        content = ai_writer.validate_reel(dict(REEL_VALID), "", "Technology")
        voice_wav = self.tmp / "voice.wav"
        subprocess.run([video_renderer.find_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=200:duration=8", str(voice_wav)], check=True)
        text = reel_studio.narration_text(content)
        vo = tts.Voiceover(voice_wav, 8.0, tts.distribute_words(text, 8.0))

        class Px:
            enabled, name = True, "pexels"
            def fetch_clips(self, q, out, wanted, **k): return clips
        res = reel_studio.make_reel(content, make_cluster(), self.tmp / "r.mp4", providers=[Px()], tts=lambda t, d: vo)
        info = video_renderer.probe(res.path)
        self.assertEqual((res.voiced, res.footage), (True, "pexels"))
        self.assertEqual((info["video"], info["audio"], info["size"]), ("h264", "aac", (1080, 1920)))
        self.assertAlmostEqual(info["duration"], reel_renderer.LEAD + 8.0 + reel_renderer.OUTRO, delta=0.5)

    def test_full_reel_without_voice_or_footage_still_moves(self):
        """Offline / no keys: animated background + reading-speed captions, never a failure."""
        content = ai_writer.validate_reel(dict(REEL_VALID), "", "Technology")
        res = reel_studio.make_reel(content, make_cluster(), self.tmp / "r2.mp4",
                                    providers=[], tts=lambda t, d: None)
        info = video_renderer.probe(res.path)
        self.assertEqual((res.voiced, res.footage), (False, "animated"))
        self.assertEqual((info["video"], info["audio"], info["size"]), ("h264", "aac", (1080, 1920)))
        self.assertGreaterEqual(info["duration"], 8.0)

    def test_broken_footage_falls_back_to_animated_background(self):
        bad = self.tmp / "bad.mp4"
        bad.write_bytes(b"not a video" * 2000)
        anim = self.tmp / "anim.mp4"
        anim.write_bytes(b"x")

        class Px:
            enabled, name = True, "pexels"
            def fetch_clips(self, q, out, wanted, **k): return [bad]

        def fake_compose(bg, content, cluster, words, total, voice, music, out):
            out.write_bytes(b"video")
            return out
        content = ai_writer.validate_reel(dict(REEL_VALID), "", "Technology")
        with mock.patch.object(reel_studio, "prepare_animated_background", return_value=anim) as animated, \
                mock.patch.object(reel_studio, "compose_reel", side_effect=fake_compose):
            res = reel_studio.make_reel(content, make_cluster(), self.tmp / "r3.mp4", providers=[Px()], tts=lambda t, d: None)
        self.assertEqual(res.footage, "animated")
        animated.assert_called_once()  # the real ffmpeg rejected the corrupt clip -> animated background used

    def test_narration_gets_follow_line_only_when_short(self):
        c = {"narration": ["one two three four five."]}
        self.assertTrue(reel_studio.narration_text(c).endswith("Follow for more updates."))
        long = {"narration": ["word " * 20 + "end."] * 4}
        self.assertNotIn("Follow for more", reel_studio.narration_text(long))


class TestFootageSources(Base):
    def pix_hit(self, i, tags="stadium, crowd, cheering", **vids):
        return {"id": i, "duration": 12, "tags": tags, "pageURL": f"https://pixabay.com/videos/x-{i}/", "videos": vids}

    def q(self, w, h, url="https://cdn.test/v.mp4"):
        return {"url": url, "width": w, "height": h}

    def test_pixabay_pick_skips_empty_large_and_4k(self):
        hit = self.pix_hit(1, large=self.q(0, 0, ""), medium=self.q(1920, 1080, "m"), small=self.q(1280, 720, "s"),
                           tiny=self.q(640, 360, "t"))
        self.assertEqual(stock.pixabay_pick(hit)["url"], "m")
        self.assertEqual(stock.pixabay_pick(self.pix_hit(2, large=self.q(3840, 2160, "4k"), medium=self.q(1280, 720, "m")))["url"], "m")
        self.assertIsNone(stock.pixabay_pick(self.pix_hit(3, tiny=self.q(640, 360, "t"))))

    def test_pixabay_fetch_sends_key_params_and_caches_24h(self):
        hits = [self.pix_hit(1, medium=self.q(1920, 1080)), self.pix_hit(2, medium=self.q(1920, 1080))]
        sess = FakeSession()

        class S(FakeSession):
            def get(s, url, headers=None, params=None, timeout=None, stream=False):
                if "api/videos" in url:
                    s.calls.append((url, headers, params))
                    return Resp(200, {"hits": hits})
                return super().get(url, headers, params, timeout, stream)
        sess = S()
        px = stock.PixabayClient("PIXKEY", session=sess)
        clips = px.fetch_clips(["stadium crowd"], self.tmp / "pc", wanted=1)
        self.assertEqual(len(clips), 1)
        self.assertEqual(sess.calls[0][2]["key"], "PIXKEY")
        n_api = len([c for c in sess.calls if "api/videos" in c[0]])
        px.fetch_clips(["stadium crowd"], self.tmp / "pc2", wanted=1)           # same query again -> served from cache
        self.assertEqual(len([c for c in sess.calls if "api/videos" in c[0]]), n_api)

    def test_provider_selection(self):
        with mock.patch.multiple(config, PEXELS_API_KEY="", PIXABAY_API_KEY="", STOCK_PROVIDER="auto"):
            self.assertEqual(stock.configured_providers(), [])
        with mock.patch.multiple(config, PEXELS_API_KEY="a", PIXABAY_API_KEY="b", STOCK_PROVIDER="auto"):
            self.assertEqual([c.name for c in stock.configured_providers()], ["pexels", "pixabay"])
        with mock.patch.multiple(config, PEXELS_API_KEY="a", PIXABAY_API_KEY="b", STOCK_PROVIDER="pixabay"):
            self.assertEqual([c.name for c in stock.configured_providers()], ["pixabay"])
        with mock.patch.multiple(config, PEXELS_API_KEY="a", PIXABAY_API_KEY="b", STOCK_PROVIDER="local"):
            self.assertEqual(stock.configured_providers(), [])

    def test_local_clips_prefer_category_folder(self):
        root = self.tmp / "broll"
        for sub, names in {"Sports": ["s1.mp4", "s2.mp4"], "technology": ["t1.mp4"]}.items():
            (root / sub).mkdir(parents=True)
            for n in names:
                (root / sub / n).write_bytes(b"0")
        (root / "loose.mp4").write_bytes(b"0")
        self.assertEqual(sorted(p.name for p in stock.local_clips(root, 9, "Sports")), ["s1.mp4", "s2.mp4"])
        self.assertEqual([p.name for p in stock.local_clips(root, 9, "Technology")], ["t1.mp4"])  # case-insensitive
        self.assertEqual([p.name for p in stock.local_clips(root, 9, "News")], ["loose.mp4"])     # falls back to loose
        only_sub = self.tmp / "b2"
        (only_sub / "x").mkdir(parents=True)
        (only_sub / "x" / "a.mov").write_bytes(b"0")
        self.assertEqual([p.name for p in stock.local_clips(only_sub, 9, "News")], ["a.mov"])      # any sub-folder

    def test_studio_falls_through_providers_then_local_then_animated(self):
        calls = []

        class Empty:
            enabled, name = True, "pexels"
            def fetch_clips(self, *a, **k): calls.append("pexels"); return []

        class Broken:
            enabled, name = True, "pixabay"
            def fetch_clips(self, *a, **k): calls.append("pixabay"); raise RuntimeError("HTTP 500")
        mine = self.tmp / "broll"
        mine.mkdir()
        (mine / "a.mp4").write_bytes(b"0")
        anim = self.tmp / "anim.mp4"
        anim.write_bytes(b"x")
        prepared = []

        def fake_footage(clips, total, tmp):
            prepared.append(list(clips))
            return anim

        def fake_compose(bg, content, cluster, words, total, voice, music, out):
            out.write_bytes(b"v")
            return out
        content = ai_writer.validate_reel(dict(REEL_VALID), "", "Technology")
        with mock.patch.object(reel_studio, "prepare_footage_background", side_effect=fake_footage), \
                mock.patch.object(reel_studio, "compose_reel", side_effect=fake_compose), \
                mock.patch.object(config, "BROLL_DIR", str(mine)):
            res = reel_studio.make_reel(content, make_cluster(), self.tmp / "o.mp4", providers=[Empty(), Broken()],
                                        tts=lambda t, d: None)
        self.assertEqual(calls, ["pexels", "pixabay"])
        self.assertEqual((res.footage, res.credit), ("local", None))
        self.assertEqual(prepared[0][0].name, "a.mp4")

    def test_credit_names(self):
        mk = lambda f: reel_studio.ReelResult(Path("x"), True, f, 9.0)  # noqa: E731
        self.assertEqual((mk("pexels").credit, mk("pixabay").credit, mk("local").credit, mk("animated").credit),
                         ("Pexels", "Pixabay", None, None))

    def test_stock_provider_none_disables_local_clips_too(self):
        mine = self.tmp / "broll"
        mine.mkdir()
        (mine / "a.mp4").write_bytes(b"0")
        anim = self.tmp / "anim.mp4"
        anim.write_bytes(b"x")

        def fake_compose(bg, content, cluster, words, total, voice, music, out):
            out.write_bytes(b"v")
            return out
        content = ai_writer.validate_reel(dict(REEL_VALID), "", "Technology")
        with mock.patch.object(reel_studio, "prepare_animated_background", return_value=anim), \
                mock.patch.object(reel_studio, "compose_reel", side_effect=fake_compose), \
                mock.patch.multiple(config, BROLL_DIR=str(mine), STOCK_PROVIDER="none"):
            res = reel_studio.make_reel(content, make_cluster(), self.tmp / "o2.mp4", providers=[], tts=lambda t, d: None)
        self.assertEqual(res.footage, "animated")


class TestRobustness(Base):
    """Issues seen in a real run: placeholder keys, a rejected key retried 8x, Gemini voice without timings."""

    def test_placeholder_keys_are_treated_as_missing(self):
        for v in ("your_pexels_key", "YOUR_KEY_HERE", "xxxx", "<paste key>", "changeme", "paste_key_here", ""):
            with mock.patch.dict(os.environ, {"PEXELS_API_KEY": v}):
                self.assertEqual(config._key("PEXELS_API_KEY"), "", v)
        with mock.patch.dict(os.environ, {"PEXELS_API_KEY": "5634abcDEF1234567890real"}):
            self.assertEqual(config._key("PEXELS_API_KEY"), "5634abcDEF1234567890real")

    def test_rejected_key_stops_after_first_request(self):
        class S401(FakeSession):
            pass
        sess = S401(status=401)
        px = stock.PexelsClient("BADKEY", session=sess)
        self.assertEqual(px.fetch_clips(["a b", "c d", "e f", "g h"], self.tmp / "x", wanted=3), [])
        self.assertEqual(len(sess.calls), 1)  # not 4: no point hammering a provider that refuses the key
        with self.assertRaises(stock.StockAuthError):
            px._items("a b")

    def test_pixabay_invalid_key_is_an_auth_error(self):
        class R(Resp):
            text = "[ERROR 400] Invalid or missing API key"

        class S:
            def get(self, url, **kw):
                return R(400)
        px = stock.PixabayClient("bad", session=S(), cache_path=self.tmp / "c.json")
        with self.assertRaises(stock.StockAuthError):
            px.search("x")

    def _speech_wav(self, parts):
        """parts: [('s', seconds) silence | ('t', seconds) tone ...] -> wav path"""
        ff = video_renderer.find_ffmpeg()
        args, labels = [], ""
        for i, (kind, sec) in enumerate(parts):
            src = f"anullsrc=r=24000:cl=mono:d={sec}" if kind == "s" else f"sine=f={300 + 100 * i}:r=24000:d={sec}"
            args += ["-f", "lavfi", "-i", src]
            labels += f"[{i}]"
        out = self.tmp / "speech.wav"
        subprocess.run([ff, "-y", "-loglevel", "error", *args, "-filter_complex",
                        f"{labels}concat=n={len(parts)}:v=0:a=1", "-c:a", "pcm_s16le", str(out)], check=True)
        return out

    def test_alignment_uses_real_pauses_between_sentences(self):
        # silence .4 | speech 1.0 | pause .6 | speech 1.5 | pause .7 | speech 1.0   (total 5.2 s)
        wav = self._speech_wav([("s", 0.4), ("t", 1.0), ("s", 0.6), ("t", 1.5), ("s", 0.7), ("t", 1.0)])
        text = "First sentence here. Second one is a bit longer. Third."
        words = tts.align_words(text, wav, 5.2)
        self.assertEqual(len(words), len(text.split()))
        self.assertAlmostEqual(words[0][1], 0.4, delta=0.2)                  # leading silence skipped
        self.assertAlmostEqual(words[3][1], 2.0, delta=0.2)                  # "Second" starts after the first pause
        self.assertAlmostEqual(words[-1][1], 4.2, delta=0.2)                 # "Third." after the second pause
        self.assertAlmostEqual(words[-1][2], 5.2, delta=0.2)
        self.assertTrue(all(a[2] <= b[1] + 1e-6 for a, b in zip(words, words[1:])))

    def test_alignment_never_raises(self):
        words = tts.align_words("One two three. Four five.", self.tmp / "missing.wav", 4.0)
        self.assertEqual(len(words), 5)
        self.assertAlmostEqual(words[-1][2], 4.0, places=2)

    def test_alignment_with_too_few_pauses_still_trims_edges(self):
        wav = self._speech_wav([("s", 0.8), ("t", 3.0), ("s", 0.5)])  # one continuous speech burst
        words = tts.align_words("Alpha beta. Gamma delta.", wav, 4.3)
        self.assertAlmostEqual(words[0][1], 0.8, delta=0.2)
        self.assertAlmostEqual(words[-1][2], 3.8, delta=0.2)


class TestFootageRelevance(Base):
    """Real failure: an election/tribunal story got hands, autumn trees, vinyl records and a slackliner."""

    def hit(self, i, tags, slug="clip"):
        return {"id": i, "duration": 12, "tags": tags, "pageURL": f"https://pixabay.com/videos/{slug}-{i}/",
                "videos": {"medium": {"url": f"https://cdn.test/{i}.mp4", "width": 1920, "height": 1080, "size": 5_000_000}}}

    def client(self, hits):
        class S(FakeSession):
            def get(s, url, headers=None, params=None, timeout=None, stream=False):
                if "api/videos" in url:
                    s.calls.append((url, headers, params))
                    return Resp(200, {"hits": hits})
                return super().get(url, headers, params, timeout, stream)
        return stock.PixabayClient("K", session=S(), cache_path=self.tmp / "sc.json")

    def test_stemmer_links_word_forms(self):
        self.assertEqual(stock.kw("voting"), stock.kw("voters"))
        self.assertEqual(stock.kw("voting"), stock.kw("vote"))
        self.assertNotEqual(stock.kw("voting"), stock.kw("cricket"))

    def test_unrelated_clips_are_rejected_and_the_relevant_one_chosen(self):
        hits = [self.hit(1, "hands, fingers, gesture, ok sign"), self.hit(2, "autumn, trees, forest, park"),
                self.hit(3, "vinyl, records, music store, shopping"), self.hit(4, "voting, election, ballot, democracy"),
                self.hit(5, "slackline, balance, sport, woman")]
        px = self.client(hits)
        clips = px.fetch_clips(["voting ballot box"], self.tmp / "o", wanted=1)
        self.assertEqual(len(clips), 1)
        self.assertEqual(px.chosen[0]["query"], "voting ballot box")
        self.assertIn("voting", px.chosen[0]["tags"])

    def test_nothing_relevant_means_no_clip_not_a_random_one(self):
        hits = [self.hit(1, "hands, fingers"), self.hit(2, "autumn, trees"), self.hit(3, "slackline, balance")]
        px = self.client(hits)
        self.assertEqual(px.fetch_clips(["voting ballot box", "courtroom gavel"], self.tmp / "o2", wanted=2), [])

    def test_best_match_beats_weaker_match(self):
        hits = [self.hit(1, "crowd, people"), self.hit(2, "stadium, crowd, cheering, fans")]
        px = self.client(hits)
        px.fetch_clips(["stadium crowd cheering"], self.tmp / "o3", wanted=1)
        self.assertIn("stadium", px.chosen[0]["tags"])

    def test_pexels_uses_the_url_slug_as_searchable_text(self):
        sess = FakeSession([pexels_video(1, [mp4_file(1080, 1920)], slug="people-voting-at-polling-station"),
                            pexels_video(2, [mp4_file(1080, 1920)], slug="woman-on-a-slackline")])
        px = stock.PexelsClient("K", session=sess)
        px.fetch_clips(["voting queue"], self.tmp / "p", wanted=1)
        self.assertIn("voting", px.chosen[0]["tags"])

    def test_topic_queries_use_concrete_scenes(self):
        q = stock.topic_queries("19,000 West Bengal voters to miss bypolls as appeals remain pending in tribunals")
        self.assertEqual(q[0], "voting ballot box")
        self.assertIn("courtroom gavel", q)                              # second matching topic: tribunals
        self.assertIn("cricket match stadium", stock.topic_queries("Kohli hits a century as India win the Test"))
        self.assertIn("circuit board", stock.topic_queries("New chip launched for laptops"))
        self.assertEqual(stock.topic_queries("Local council meets on Tuesday"), [])

    def test_queries_for_orders_specific_then_ai_then_category(self):
        q = stock.queries_for("News", ["functional issues", "legal documents"], "voters bypolls tribunal")
        self.assertEqual(q[0], "voting ballot box")
        self.assertNotIn("functional", " ".join(q))                      # abstract words are never searched
        self.assertLess(q.index("legal documents"), q.index("city street traffic"))
        self.assertLessEqual(len(q), 10)

    def test_clean_query_drops_abstract_words(self):
        self.assertEqual(stock.clean_query("functional issues pending appeals"), "")
        self.assertEqual(stock.clean_query("Voting Ballot issues!"), "voting ballot")

    def test_pixabay_pick_skips_huge_files(self):
        hit = {"videos": {"large": {"url": "big", "width": 1920, "height": 1080, "size": 90_000_000},
                          "medium": {"url": "ok", "width": 1280, "height": 720, "size": 8_000_000}}}
        self.assertEqual(stock.pixabay_pick(hit)["url"], "ok")

    def test_local_clip_names_matching_the_story_come_first(self):
        root = self.tmp / "broll2"
        (root / "News").mkdir(parents=True)
        for name in ("random_clip.mp4", "city_night.mp4"):
            (root / "News" / name).write_bytes(b"0")
        (root / "election_voting_queue.mp4").write_bytes(b"0")
        got = stock.local_clips(root, 3, "News", queries=["voting ballot box", "people voting queue"])
        self.assertEqual(got[0].name, "election_voting_queue.mp4")

    def test_studio_searches_with_story_specific_terms(self):
        seen = []

        class Spy:
            enabled, name, chosen = True, "pixabay", []
            def fetch_clips(self, queries, out, wanted, **k):
                seen.append(list(queries))
                return []
        c = make_cluster("News")
        c["lead"] = dict(c["lead"], title="19,000 voters to miss bypolls as appeals pending in tribunals",
                         summary="Voters in West Bengal remain unable to vote.")
        anim = self.tmp / "a.mp4"
        anim.write_bytes(b"x")

        def fake_compose(bg, content, cluster, words, total, voice, music, out):
            out.write_bytes(b"v")
            return out
        content = ai_writer.validate_reel(dict(REEL_VALID, broll_queries=["functional issues"]), "", "News")
        with mock.patch.object(reel_studio, "prepare_animated_background", return_value=anim), \
                mock.patch.object(reel_studio, "compose_reel", side_effect=fake_compose):
            reel_studio.make_reel(content, c, self.tmp / "o.mp4", providers=[Spy()], tts=lambda t, d: None)
        self.assertEqual(seen[0][0], "voting ballot box")
        self.assertNotIn("functional", " ".join(seen[0]))


class TestDynamicReelPipeline(PipelineFixture):
    def maker(self, footage="pexels", fail=False):
        calls = []

        def make(content, cand, out_path, music_path=None):
            calls.append(content)
            if fail:
                raise video_renderer.VideoError("render exploded")
            subprocess.run([video_renderer.find_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=s=320x568:r=10:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)], check=True)
            return reel_studio.ReelResult(Path(out_path), True, footage, 8.0)
        make.calls = calls
        return make

    def run_dynamic(self, maker):
        ai = lambda p: json.dumps(REEL_VALID if "VOICEOVER" in p else VALID)  # noqa: E731
        p = self.build(ai=ai)
        p.reel_maker = maker
        with mock.patch.multiple(config, CONTENT_FORMATS={"reel": 1}, REEL_STYLE="dynamic"):
            return p, p.run_cycle()

    def test_dynamic_reel_published_with_narration_content_and_credit(self):
        m = self.maker("pexels")
        p, res = self.run_dynamic(m)
        self.assertEqual((res["status"], res["format"]), ("published", "reel"), res)
        self.assertIn("narration", m.calls[0])                       # the voiced script, not info cards
        create = next(c for c in self.ig_session.calls if c[0] == "POST" and c[2].get("media_type") == "REELS")
        self.assertIn("Footage: Pexels", create[2]["caption"])
        self.assertEqual(p.load_history()[0]["reel"], "voice=True footage=pexels")

    def test_no_pexels_credit_when_footage_not_from_pexels(self):
        p, res = self.run_dynamic(self.maker("animated"))
        create = next(c for c in self.ig_session.calls if c[0] == "POST" and c[2].get("media_type") == "REELS")
        self.assertNotIn("Footage:", create[2]["caption"])

    def test_reel_failure_falls_back_to_carousel(self):
        p, res = self.run_dynamic(self.maker(fail=True))
        self.assertEqual((res["status"], res["format"]), ("published", "carousel"), res)
        self.assertEqual(self.ig_session.published, 1)

    def test_legacy_cards_style_still_available(self):
        p = self.build()
        with mock.patch.multiple(config, CONTENT_FORMATS={"reel": 1}, REEL_STYLE="cards"):
            res = p.run_cycle()
        self.assertEqual((res["status"], res["format"]), ("published", "reel"), res)


class TestOwnClipPosting(PipelineFixture):
    """Publish a clip the operator has rights to: reformat, credit, caption, publish. Never downloads anything."""

    def make_video(self, name, w, h, seconds=5, audio=True):
        ff = video_renderer.find_ffmpeg()
        out = self.tmp / name
        cmd = [ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r=25:d={seconds}"]
        if audio:
            cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)]
        subprocess.run(cmd, check=True)
        return out

    def cluster(self):
        return clip_post.synthetic_cluster("India win Asian Games cricket gold medals", "", "BCCI / Star Sports", "Sports")

    def test_landscape_clip_becomes_9x16_and_keeps_original_audio(self):
        src = self.make_video("land.mp4", 1280, 720, 5, audio=True)
        out = clip_post.prepare_clip(src, self.tmp / "o.mp4", cluster=self.cluster(), hook="India win gold", credit="BCCI", fps=10)
        info = video_renderer.probe(out)
        self.assertEqual((info["video"], info["audio"], info["size"]), ("h264", "aac", (1080, 1920)))
        self.assertAlmostEqual(info["duration"], 5.0, delta=0.4)

    def test_silent_clip_gets_an_audio_track_and_long_clip_is_trimmed(self):
        src = self.make_video("silent.mp4", 720, 1280, 6, audio=False)
        out = clip_post.prepare_clip(src, self.tmp / "o2.mp4", cluster=self.cluster(), hook=None, credit="X Org",
                                     max_seconds=4, fps=10)
        info = video_renderer.probe(out)
        self.assertEqual(info["audio"], "aac")
        self.assertAlmostEqual(info["duration"], 4.0, delta=0.4)

    def test_hook_headline_only_shows_at_the_start(self):
        src = self.make_video("hook.mp4", 1280, 720, 6, audio=True)
        out = clip_post.prepare_clip(src, self.tmp / "o3.mp4", cluster=self.cluster(), hook="India win Asian Games gold",
                                     credit="BCCI", fps=10)
        ff = video_renderer.find_ffmpeg()
        frames = {}
        for t in ("1.0", "5.0"):
            png = self.tmp / f"f{t}.png"
            subprocess.run([ff, "-y", "-loglevel", "error", "-ss", t, "-i", str(out), "-frames:v", "1", str(png)], check=True)
            frames[t] = Image.open(png).convert("RGB").crop((96, 420, 900, 520))
        # white headline pixels exist at t=1s, none at t=5s
        white = lambda im: im.convert("L").point(lambda v: 255 if v > 245 else 0).histogram()[255]  # noqa: E731
        self.assertGreater(white(frames["1.0"]), 200)
        self.assertLess(white(frames["5.0"]), 50)

    def test_bad_inputs_have_clear_errors(self):
        with self.assertRaises(clip_post.ClipError):
            clip_post.prepare_clip(self.tmp / "nope.mp4", self.tmp / "x.mp4", cluster=self.cluster(), hook=None, credit="c")
        junk = self.tmp / "junk.mp4"
        junk.write_bytes(b"not a video" * 1000)
        with self.assertRaises(clip_post.ClipError):
            clip_post.prepare_clip(junk, self.tmp / "x.mp4", cluster=self.cluster(), hook=None, credit="c")
        tiny = self.make_video("tiny.mp4", 320, 568, 2)
        with self.assertRaises(clip_post.ClipError):
            clip_post.prepare_clip(tiny, self.tmp / "x.mp4", cluster=self.cluster(), hook=None, credit="c")

    def test_news_matching_grounds_the_caption(self):
        arts = [make_article(1, "Sports", "BBC Sport", now=self.NOW), make_article(2, "News", "BBC News", now=self.NOW)]
        arts[0]["title"] = "India women and men win Asian Games cricket gold medals"
        arts[0]["summary"] = "India's cricket teams won gold at the Asian Games in a double celebration for the squads."
        got = clip_post.match_news_cluster("India cricket teams win Asian Games gold", arts)
        self.assertIsNotNone(got)
        self.assertIn("Asian Games", got["lead"]["title"])
        self.assertIsNone(clip_post.match_news_cluster("Quarterly inflation numbers released", arts))

    def test_publishes_reel_with_credit_and_records_history(self):
        arts = [make_article(1, "Sports", "BBC Sport", now=self.NOW)]
        arts[0]["title"] = "India women and men win Asian Games cricket gold medals"
        arts[0]["summary"] = "India's cricket teams won gold at the Asian Games in a double celebration for the squads."
        p = self.build(articles=arts)
        src = self.make_video("c.mp4", 1280, 720, 5)
        with mock.patch.object(config, "VIDEO_FPS", 10):
            res = clip_post.post_clip(p, video=src, topic="India cricket teams win Asian Games gold",
                                      credit="BCCI / Star Sports")
        self.assertEqual((res["status"], res["format"]), ("published", "reel"), res)
        create = next(c for c in self.ig_session.calls if c[0] == "POST" and c[2].get("media_type") == "REELS")
        self.assertIn("Footage: BCCI / Star Sports", create[2]["caption"])
        self.assertIn("BBC Sport", create[2]["caption"])           # real reporting credited too
        rec = p.load_history()[0]
        self.assertIn("own clip", rec["reel"])
        self.assertTrue(rec["articles"])                              # scheduled posts will not repeat this story
        left = list((self.tmp / "public").glob("*")) if (self.tmp / "public").exists() else []
        self.assertEqual(left, [])

    def test_dry_run_and_validation(self):
        p = self.build(articles=[])
        src = self.make_video("d.mp4", 720, 1280, 5)

        def fake_prepare(video, out, **kw):
            Path(out).write_bytes(b"0" * 20_000)
            return Path(out)
        with mock.patch.object(clip_post, "prepare_clip", side_effect=fake_prepare):
            res = clip_post.post_clip(p, video=src, topic="India cricket teams win Asian Games gold",
                                      credit="BCCI", dry=True, from_news=False, category="Sports")
        self.assertEqual(res["status"], "dry_run")
        self.assertTrue(Path(res["files"][0]).exists())
        self.assertEqual(self.ig_session.published, 0)
        with self.assertRaises(clip_post.ClipError):
            clip_post.post_clip(p, video=src, topic="short", credit="BCCI")
        with self.assertRaises(clip_post.ClipError):
            clip_post.post_clip(p, video=src, topic="India cricket teams win Asian Games gold", credit="  ")

    def test_cli_refuses_without_rights_confirmation(self):
        import main
        argv = ["post-clip", "--video", str(self.tmp / "x.mp4"), "--topic", "India win Asian Games gold", "--credit", "BCCI"]
        self.assertEqual(main.main(argv), 2)


# ------------------------------------------------------------------ regression guards for the old Telegram project's issues
class TestRegressions(unittest.TestCase):
    def test_cron_comment_matches_expression(self):
        y = (ROOT / ".github/workflows/instagram_agent.yml").read_text()
        line = next(l for l in y.splitlines() if "cron:" in l)
        self.assertIn("*/30", line)
        self.assertIn("every 30 minutes", line)

    def test_no_deprecated_gemini_sdk_or_model(self):
        for f in list(ROOT.rglob("*.py")) + [ROOT / "requirements.txt"]:
            if f.name == "test_agent.py":
                continue
            txt = f.read_text()
            self.assertNotIn("google.generativeai", txt, f)
            self.assertNotIn("google-generativeai", txt, f)
            self.assertNotIn("gemini-1.5", txt, f)

    def test_single_secret_names_no_aliases(self):
        txt = (ROOT / "core/config.py").read_text()
        self.assertNotIn("BOT_KEYFILE", txt)
        self.assertNotIn("SECRETFILE", txt)

    def test_state_files_are_not_cache_blobs(self):
        gi = (ROOT / ".gitignore").read_text()
        self.assertIn("data/ai_cache.json", gi)
        y = (ROOT / ".github/workflows/instagram_agent.yml").read_text()
        self.assertNotIn("git add data/ai_cache", y)
        self.assertNotIn("git add -A", y)

    def test_readme_documents_instagram_not_telegram(self):
        r = (ROOT / "README.md").read_text()
        self.assertIn("Instagram", r)
        self.assertNotIn("Telegram", r)

    def test_config_validation(self):
        with mock.patch.multiple(config, IG_USER_ID="", IG_ACCESS_TOKEN="", MEDIA_HOST="cloudinary"):
            probs = config.validate("publish")
            self.assertTrue(any("IG_USER_ID" in p for p in probs))
            self.assertTrue(any("CLOUDINARY" in p for p in probs))
        with mock.patch.object(config, "POST_TIMES", ["25:99"]):
            self.assertTrue(any("HH:MM" in p for p in config.validate("dry")))


if __name__ == "__main__":
    unittest.main()
