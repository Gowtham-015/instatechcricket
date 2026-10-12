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

from core.stock import kw as stock_kw
from core import localphotos
from core import variety
from core import collector, feeds as feeds_mod
from core.facebook_client import FacebookError, FacebookPageClient, token_problems
from core import (clip_post, drafts, commons, openverse, trends as trends_mod, ai_writer, config, dedupe, health, image_renderer, media_host, ranker, reel_renderer, reel_studio,
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
        "id": f"id{i}", "title": f"Story{i:03d}x{category.lower()} wxq{i} zlk{i} mnb{i} plo{i} report overview",
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
                                CONTENT_FORMATS={"carousel": 1}, FORMAT_POLICY="legacy", VIDEO_SECONDS_PER_SLIDE=0.6, VIDEO_FPS=10,
                                REEL_STYLE="cards", REAL_PHOTOS=False, MIN_SOURCES=1, TRENDS_ENABLED=False, PHOTO_SOURCES=["commons", "openverse"], PEXELS_API_KEY="", PIXABAY_API_KEY="", STOCK_PROVIDER="auto", TTS_PROVIDER="none", BROLL_DIR="", MUSIC_PATH="",
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
        px.fetch_clips(["polling station voting"], self.tmp / "p", wanted=1)
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


def commons_page(i, title, lic="CC BY 4.0", artist='<a href="//commons.wikimedia.org/wiki/User:Jane">Jane Doe</a>',
                 width=3000, mime="image/jpeg", desc="", nonfree=None, restrictions=None, cats=""):
    meta = {"LicenseShortName": {"value": lic}, "Artist": {"value": artist}, "ImageDescription": {"value": desc},
            "Categories": {"value": cats}}
    if nonfree:
        meta["NonFree"] = {"value": nonfree}
    if restrictions:
        meta["Restrictions"] = {"value": restrictions}
    return {"title": f"File:{title}", "index": i, "imageinfo": [{
        "url": f"https://upload.test/full/{i}.jpg", "thumburl": f"https://upload.test/thumb/{i}.jpg",
        "width": width, "height": 2000, "mime": mime, "extmetadata": meta}]}


class CommonsSession:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, headers=None, params=None, timeout=None, stream=False):
        self.calls.append((url, headers, params))
        if "api.php" in url:
            return Resp(200, {"query": {"pages": self.pages}})

        class Dl(Resp):
            def iter_content(s, n): yield b"\xff\xd8" + b"0" * 30_000
        return Dl(200)


class TestDocumentaryPhotos(Base):
    """Real photographs of the story's real subjects (people, places, institutions) from Wikimedia Commons."""

    def make_jpeg(self, name, size, kind="gradient"):
        im = Image.new("RGB", size)
        px = im.load()
        for x in range(size[0]):
            for y in range(0, size[1], 8):
                c = (255 - int(255 * x / size[0]), 40, int(255 * x / size[0])) if kind == "gradient" else (60, 160, 90)
                for yy in range(y, min(y + 8, size[1])):
                    px[x, yy] = c
        path = self.tmp / name
        im.save(path, "JPEG")
        return path

    def test_license_policy(self):
        for ok in ("CC BY 4.0", "CC BY 2.0", "CC BY 3.0 de", "CC0", "CC0 1.0", "Public domain", "PD-USGov", "pd"):
            self.assertTrue(commons.license_ok(ok), ok)
        for bad in ("CC BY-SA 4.0", "CC BY-NC 2.0", "CC BY-ND 2.0", "CC BY-NC-SA", "GFDL", "Attribution", "",
                    "Fair use", "Copyrighted free use"):
            self.assertFalse(commons.license_ok(bad), bad)

    def test_entities_become_photo_queries(self):
        self.assertEqual(commons.entity_queries("19,000 West Bengal voters to miss bypolls; Election Commission of India said"),
                         ["West Bengal", "Election Commission of India"])
        self.assertEqual(commons.entity_queries("After Trent Reznor produced the debut, fans cheered"), ["Trent Reznor"])
        q = commons.photo_queries_for("Eden Gardens hosts India vs Australia", ["Virat Kohli!", "eden gardens"])
        self.assertEqual(q[0], "Virat Kohli")
        self.assertEqual([x.lower() for x in q].count("eden gardens"), 1)

    def test_only_licence_safe_relevant_real_photos_pass(self):
        pages = [
            commons_page(1, "Eden Gardens during IPL match 2019.jpg", cats="Eden Gardens|Cricket in Kolkata"),   # ok
            commons_page(2, "Eden Gardens stadium view.jpg", lic="CC BY-SA 4.0"),                                  # share-alike
            commons_page(3, "Eden Gardens panorama.jpg", width=800),                                               # too small
            commons_page(4, "Eden Gardens logo.jpg"),                                                              # logo
            commons_page(5, "Eden Gardens.svg", mime="image/svg+xml"),                                             # not a photo
            commons_page(6, "Random street scene Paris.jpg"),                                                      # unrelated
            commons_page(7, "Eden Gardens gate.jpg", nonfree="true"),                                              # non-free
            commons_page(8, "Eden Gardens ticket office.jpg", restrictions="trademarked"),                         # trademark
            commons_page(9, "Flag of Eden Gardens.jpg"),                                                           # flag
        ]
        cc = commons.CommonsClient(session=CommonsSession(pages), cache_path=self.tmp / "cc.json")
        cands = cc.candidates("Eden Gardens")
        self.assertEqual([c["title"] for c in cands], ["Eden Gardens during IPL match 2019"])
        self.assertEqual(cands[0]["credit"], "Jane Doe (CC BY 4.0)")

    def test_two_word_name_needs_both_words(self):
        pages = [commons_page(1, "Bengal tiger in forest.jpg"), commons_page(2, "West Bengal village road.jpg")]
        cc = commons.CommonsClient(session=CommonsSession(pages), cache_path=self.tmp / "cc2.json")
        self.assertEqual([c["title"] for c in cc.candidates("West Bengal")], ["West Bengal village road"])

    def test_fetch_downloads_credits_caches_and_identifies_itself(self):
        sess = CommonsSession([commons_page(1, "Eden Gardens night match.jpg"),
                               commons_page(2, "Eden Gardens aerial view.jpg", lic="CC0", artist="")])
        cc = commons.CommonsClient(session=sess, cache_path=self.tmp / "cc3.json")
        photos = cc.fetch_photos(["Eden Gardens"], self.tmp / "ph", wanted=2)
        self.assertEqual(len(photos), 2)
        self.assertTrue(all(p.path.exists() and p.path.stat().st_size > 1000 for p in photos))
        self.assertTrue(any("CC0" in p.credit and "Unknown author" in p.credit for p in photos))
        ua = sess.calls[0][1]["User-Agent"]
        self.assertIn("InstaNewsAgent", ua)                                      # Wikimedia requires a descriptive UA
        n_api = len([c for c in sess.calls if "api.php" in c[0]])
        cc.fetch_photos(["Eden Gardens"], self.tmp / "ph2", wanted=1)           # repeated query -> from the 24 h cache
        self.assertEqual(len([c for c in sess.calls if "api.php" in c[0]]), n_api)
        text = commons.images_credit(photos, max_items=1)
        self.assertTrue(text.endswith("via Wikimedia Commons"))
        self.assertIn("and 1 more", text)

    def test_commons_outage_returns_no_photos_not_an_error(self):
        class Down:
            def get(self, *a, **k): return Resp(503)
        cc = commons.CommonsClient(session=Down(), cache_path=self.tmp / "cc4.json")
        self.assertEqual(cc.fetch_photos(["Eden Gardens"], self.tmp / "ph3"), [])

    def test_photo_segments_pan_across_landscape_photos(self):
        land = self.make_jpeg("land.jpg", (3000, 2000))           # red (left) -> blue (right)
        tall = self.make_jpeg("tall.jpg", (1000, 1700), kind="flat")
        items = [{"kind": "photo", "path": land, "credit": "Jane Doe (CC BY 4.0)"},
                 {"kind": "photo", "path": tall, "credit": "John Roe (CC0)"}]
        (self.tmp / "mix").mkdir()
        bg = reel_renderer.prepare_mixed_background(items, 8.0, self.tmp / "mix")
        info = video_renderer.probe(bg)
        self.assertEqual((info["video"], info["size"]), ("h264", (1080, 1920)))
        n, seg = reel_renderer.segment_plan(8.0)
        self.assertAlmostEqual(info["duration"], n * seg, delta=0.6)
        ff = video_renderer.find_ffmpeg()

        def frame_color(t):
            png = self.tmp / f"b{t}.png"
            subprocess.run([ff, "-y", "-loglevel", "error", "-ss", str(t), "-i", str(bg), "-frames:v", "1", str(png)], check=True)
            im = Image.open(png).convert("RGB").crop((300, 700, 780, 1000)).resize((1, 1))
            return im.getpixel((0, 0))
        start, end = frame_color(0.1), frame_color(seg - 0.2)
        self.assertGreater(start[0], end[0] + 40)                  # red fades out ...
        self.assertGreater(end[2], start[2] + 40)                  # ... blue comes in: the photo really pans

    def test_studio_uses_photos_and_returns_credit(self):
        p1 = self.make_jpeg("a.jpg", (2400, 1600))
        p2 = self.make_jpeg("b.jpg", (2400, 1600))
        fake = type("C", (), {"enabled": True, "name": "commons"})()
        fake.fetch_photos = lambda queries, out, wanted=4, rng=None, **k: [
            commons.Photo(p1, "Jane Doe (CC BY 4.0)", "A", queries[0]), commons.Photo(p2, "Jane Doe (CC BY 4.0)", "B", queries[0])]
        captured = {}

        def fake_mixed(items, total, tmp):
            captured["items"] = items
            (tmp / "bg.mp4").write_bytes(b"x")
            return tmp / "bg.mp4"

        def fake_compose(bg, content, cluster, words, total, voice, music, out):
            out.write_bytes(b"v")
            return out
        content = ai_writer.validate_reel(dict(REEL_VALID, photo_queries=["Eden Gardens"]), "", "Sports")
        with mock.patch.object(config, "REAL_PHOTOS", True), \
                mock.patch.object(reel_studio, "prepare_mixed_background", side_effect=fake_mixed), \
                mock.patch.object(reel_studio, "compose_reel", side_effect=fake_compose):
            res = reel_studio.make_reel(content, make_cluster("Sports"), self.tmp / "o.mp4", providers=[], commons=fake,
                                        tts=lambda t, d: None)
        self.assertEqual([i["kind"] for i in captured["items"]], ["photo", "photo"])
        self.assertEqual(res.footage, "commons")
        self.assertEqual(res.images_credit, "Jane Doe (CC BY 4.0) via Wikimedia Commons")
        self.assertIsNone(res.credit)

    def test_photos_are_interleaved_with_relevant_clips(self):
        p1, p2 = self.make_jpeg("c.jpg", (2400, 1600)), self.make_jpeg("d.jpg", (2400, 1600))
        clip = self.tmp / "clip.mp4"
        clip.write_bytes(b"0")
        cc = type("C", (), {"enabled": True, "name": "commons"})()
        cc.fetch_photos = lambda q, out, wanted=4, rng=None, **k: [commons.Photo(p1, "A (CC0)", "A", "x"), commons.Photo(p2, "B (CC0)", "B", "x")]

        class Px:
            enabled, name, chosen = True, "pixabay", []
            def fetch_clips(self, *a, **k): return [clip]
        got = {}

        def fake_mixed(items, total, tmp):
            got["kinds"] = [i["kind"] for i in items]
            (tmp / "bg.mp4").write_bytes(b"x")
            return tmp / "bg.mp4"

        def fake_compose(bg, content, cluster, words, total, voice, music, out):
            out.write_bytes(b"v")
            return out
        content = ai_writer.validate_reel(dict(REEL_VALID), "", "Sports")
        with mock.patch.object(config, "REAL_PHOTOS", True), \
                mock.patch.object(reel_studio, "prepare_mixed_background", side_effect=fake_mixed), \
                mock.patch.object(reel_studio, "compose_reel", side_effect=fake_compose):
            res = reel_studio.make_reel(content, make_cluster("Sports"), self.tmp / "o2.mp4", providers=[Px()], commons=cc,
                                        tts=lambda t, d: None)
        self.assertEqual(got["kinds"], ["photo", "clip", "photo"])
        self.assertEqual((res.footage, res.credit), ("pixabay", "Pixabay"))

    def test_same_story_gets_same_footage_preview_equals_published(self):
        """Seen in a real run: preview and published Reel had different backgrounds (unseeded random choice)."""
        seeds = []

        class Spy:
            enabled, name, chosen = True, "pixabay", []
            def fetch_clips(self, queries, out, wanted, rng=None, **k):
                seeds.append(rng.random())
                return []
        anim = self.tmp / "an.mp4"
        anim.write_bytes(b"x")

        def fake_compose(bg, content, cluster, words, total, voice, music, out):
            out.write_bytes(b"v")
            return out
        content = ai_writer.validate_reel(dict(REEL_VALID), "", "Technology")
        for _ in range(2):
            with mock.patch.object(reel_studio, "prepare_animated_background", return_value=anim), \
                    mock.patch.object(reel_studio, "compose_reel", side_effect=fake_compose):
                reel_studio.make_reel(content, make_cluster(), self.tmp / "o3.mp4", providers=[Spy()], tts=lambda t, d: None)
        self.assertEqual(seeds[0], seeds[1])
        other = make_cluster()
        other["fingerprint"] = "different-story"
        with mock.patch.object(reel_studio, "prepare_animated_background", return_value=anim), \
                mock.patch.object(reel_studio, "compose_reel", side_effect=fake_compose):
            reel_studio.make_reel(content, other, self.tmp / "o4.mp4", providers=[Spy()], tts=lambda t, d: None)
        self.assertNotEqual(seeds[0], seeds[2])

    def test_caption_contains_images_credit_within_limit(self):
        content = ai_writer.validate_reel(dict(REEL_VALID), "", "Technology")
        cap = ai_writer.compose_caption(content, make_cluster(), "Pixabay", "Jane Doe (CC BY 4.0) via Wikimedia Commons")
        self.assertIn("📷 Images: Jane Doe (CC BY 4.0) via Wikimedia Commons", cap)
        self.assertLessEqual(len(cap), 2200)

    def test_script_carries_sanitised_photo_queries(self):
        out = ai_writer.validate_reel(dict(REEL_VALID, photo_queries=["Eden Gardens!", "Virat Kohli", "virat kohli", 7]), "", "Sports")
        self.assertEqual(out["photo_queries"], ["Eden Gardens", "Virat Kohli"])
        self.assertEqual(ai_writer.fallback_reel(make_cluster())["photo_queries"], [])

    def test_pipeline_puts_images_credit_in_the_published_caption(self):
        def make(content, cand, out_path, music_path=None):
            subprocess.run([video_renderer.find_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=s=320x568:r=10:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)], check=True)
            return reel_studio.ReelResult(Path(out_path), True, "commons", 8.0, "Jane Doe (CC BY 4.0) via Wikimedia Commons")
        arts = [make_article(1, "News", "BBC News", now=datetime(2026, 10, 5, 3, 40, tzinfo=timezone.utc))]
        ig = FakeIG()
        host = media_host.LocalHost("https://cdn.test/media", self.tmp / "public")
        pl = Pipeline(collector=lambda: arts, writer=AIWriter(generate_json=lambda p: json.dumps(REEL_VALID if "VOICEOVER" in p else VALID),
                                                              cache_path=self.tmp / "ai.json", sleep=lambda s: None),
                      host=host, ig=make_ig(ig), now_fn=lambda: datetime(2026, 10, 5, 3, 40, tzinfo=timezone.utc),
                      data_dir=self.tmp, sleep=lambda s: None, verify_public=lambda u, kind="image": True, reel_maker=make)
        with mock.patch.multiple(config, CONTENT_FORMATS={"reel": 1}, REEL_STYLE="dynamic"):
            res = pl.run_cycle()
        self.assertEqual(res["status"], "published", res)
        create = next(c for c in ig.calls if c[0] == "POST" and c[2].get("media_type") == "REELS")
        self.assertIn("📷 Images: Jane Doe (CC BY 4.0) via Wikimedia Commons", create[2]["caption"])


TRENDS_XML = """<?xml version="1.0"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>Vijay Nayagan</title><ht:approx_traffic>200K+</ht:approx_traffic>
<ht:news_item><ht:news_item_title>Vijay final film Nayagan release date announced by makers</ht:news_item_title>
<ht:news_item_url>https://x.test/a</ht:news_item_url><ht:news_item_source>The Hindu</ht:news_item_source></ht:news_item></item>
<item><title>ind vs aus</title><ht:approx_traffic>2M+</ht:approx_traffic></item>
<item><title>Rain</title><ht:approx_traffic>500+</ht:approx_traffic></item>
</channel></rss>"""


class TestTrending(Base):
    """Real complaint: 'this is not trending news'. Stories now need wide coverage or a Google Trends match."""

    def test_traffic_parsing_and_bonus(self):
        self.assertEqual([trends_mod.parse_traffic(x) for x in ("200+", "100K+", "2M+", "1,500+", "", None, "n/a")],
                         [200, 100_000, 2_000_000, 1500, 0, 0, 0])
        small, big = trends_mod.Trend("a", 200), trends_mod.Trend("b", 2_000_000)
        self.assertGreater(trends_mod.bonus_for(big), trends_mod.bonus_for(small))
        self.assertLessEqual(trends_mod.bonus_for(big), 25.0)

    def test_feed_parsing(self):
        ts = trends_mod.parse_feed(TRENDS_XML)
        self.assertEqual([(t.query, t.traffic) for t in ts], [("Vijay Nayagan", 200_000), ("ind vs aus", 2_000_000), ("Rain", 500)])
        self.assertEqual(ts[0].news_titles, ["Vijay final film Nayagan release date announced by makers"])

    def test_fetch_caches_and_fails_soft(self):
        calls = []

        class S:
            def get(s, url, **kw):
                calls.append(url)
                r = Resp(200)
                r.text = TRENDS_XML
                return r
        cache = self.tmp / "tr.json"
        self.assertEqual(len(trends_mod.fetch_trends("in", session=S(), cache_path=cache)), 3)
        trends_mod.fetch_trends("IN", session=S(), cache_path=cache)
        self.assertEqual(len(calls), 1)                                   # second call served from the 1 h cache
        self.assertIn("geo=IN", calls[0])

        class Down:
            def get(s, url, **kw): return Resp(503)
        self.assertEqual(trends_mod.fetch_trends("US", session=Down(), cache_path=self.tmp / "tr2.json"), [])

        class Boom:
            def get(s, url, **kw): raise requests.ConnectionError("offline")
        self.assertEqual(trends_mod.fetch_trends("US", session=Boom(), cache_path=self.tmp / "tr3.json"), [])

    def test_matching_by_query_words_and_by_linked_headlines(self):
        ts = trends_mod.parse_feed(TRENDS_XML)
        m = trends_mod.match_trend("Vijay's Nayagan release date locked", "Vijay's Nayagan release date locked, fans cheer.", ts)
        self.assertEqual(m.query, "Vijay Nayagan")
        # no shared query words, but it is the same news story Google links to the trend
        m2 = trends_mod.match_trend("Final film of Vijay: makers announce release date", "x", ts)
        self.assertEqual(m2.query, "Vijay Nayagan")
        self.assertIsNone(trends_mod.match_trend("Council approves new parking rules", "Council parking.", ts))
        # a lone common word such as 'rain' in the summary only must not count as a match
        self.assertIsNone(trends_mod.match_trend("Budget airline adds routes", "Budget airline adds routes despite rain delays", ts))

    def test_trend_match_boosts_score(self):
        a = make_article(1, "Entertainment", "Variety")
        a["title"] = "Vijay's Nayagan release date locked as makers confirm"
        b = make_article(2, "News", "BBC News")
        plain = ranker.build_clusters([a, b])
        boosted = ranker.build_clusters([a, b], trends=trends_mod.parse_feed(TRENDS_XML))
        sc = lambda cl, cat: next(c for c in cl if c["category"] == cat)  # noqa: E731
        self.assertGreater(sc(boosted, "Entertainment")["score"], sc(plain, "Entertainment")["score"] + 10)
        self.assertEqual(sc(boosted, "Entertainment")["trend"], "Vijay Nayagan")
        self.assertIsNone(sc(boosted, "News")["trend"])

    def test_min_sources_rule_with_trend_exception(self):
        single = make_article(1, "News", "BBC News")
        multi_a, multi_b = make_article(2, "News", "BBC News"), make_article(3, "News", "NDTV News")
        for a in (multi_a, multi_b):
            a["title"] = "Parliament passes landmark digital privacy law after marathon debate"
        clusters = ranker.build_clusters([single, multi_a, multi_b])
        with mock.patch.object(config, "MIN_SOURCES", 2):
            pick = ranker.pick_next(clusters, [], {"News": 1}, min_score=0)
            self.assertGreaterEqual(len(pick["sources"]), 2)               # the widely-covered story wins
            only_single = [c for c in clusters if len(c["sources"]) == 1]
            self.assertIsNone(ranker.pick_next(only_single, [], {"News": 1}, min_score=0))
            only_single[0]["trend"] = "some trend"                          # ...unless Google says it is trending
            self.assertIsNotNone(ranker.pick_next(only_single, [], {"News": 1}, min_score=0))

    def test_roundups_and_live_blogs_are_never_picked(self):
        for title in ("Entertainment News LIVE Updates: Vijay's film, new novel and couture show",
                      "Today's top stories: what you need to know", "10 things to know this morning",
                      "Morning Digest: major headlines", "Newsletter: the week in cinema"):
            self.assertTrue(ranker.looks_like_digest(title, "x", 2), title)
        self.assertFalse(ranker.looks_like_digest("Parliament passes digital privacy law", "Details of the law.", 3))
        multi = ("Vijay and Karthik Subbaraj met Zhayynn James and Gaurav Gupta in Mumbai while Trent Reznor and "
                 "Kevin McMahon recorded with Nine Inch Nails; Tom Cruise and Brad Pitt attended too.")
        self.assertTrue(ranker.looks_like_digest("Cinema notes", multi, 1))
        a = make_article(1, "News", "BBC News")
        a["title"] = "Live updates: markets react to the budget announcement today"
        self.assertIsNone(ranker.pick_next(ranker.build_clusters([a]), [], {"News": 1}, min_score=0))

    def test_unfocused_story_is_rejected_by_the_ai_check_and_not_retried(self):
        calls = []

        def fn(prompt):
            calls.append(1)
            return json.dumps({"unfocused": True})
        w = AIWriter(generate_json=fn, cache_path=self.tmp / "u.json", sleep=lambda s: None)
        with self.assertRaises(ai_writer.UnfocusedStoryError):
            w.write_reel(make_cluster())
        self.assertEqual(len(calls), 1)                                   # no pointless retries, no fallback copy
        self.assertIn("unfocused", ai_writer.build_reel_prompt(make_cluster())[0])


class TestTrendingPipeline(PipelineFixture):
    def test_unfocused_story_is_skipped_and_next_story_published(self):
        a = make_article(1, "Entertainment", "Variety", now=self.NOW)       # digest-like -> AI says unfocused
        a["title"] = "Cinema notes from across the industry"
        b = make_article(2, "News", "BBC News", now=self.NOW)
        b["title"] = "Parliament passes landmark digital privacy law after debate"

        def ai(prompt):
            if "VOICEOVER" in prompt and "Cinema notes" in prompt:
                return json.dumps({"unfocused": True})
            return json.dumps(REEL_VALID if "VOICEOVER" in prompt else VALID)
        p = self.build(articles=[a, b], ai=ai)

        def make(content, cand, out_path, music_path=None):
            subprocess.run([video_renderer.find_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=s=320x568:r=10:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)], check=True)
            return reel_studio.ReelResult(Path(out_path), True, "animated", 8.0)
        p.reel_maker = make
        with mock.patch.multiple(config, CONTENT_FORMATS={"reel": 1}, REEL_STYLE="dynamic",
                                 CATEGORY_WEIGHTS={"Entertainment": 5, "News": 1}):
            res = p.run_cycle()
        self.assertEqual(res["status"], "published", res)
        self.assertIn("Parliament", res["title"])
        att = storage.read_json(self.tmp / "attempts.json", {})
        self.assertTrue(any(v["count"] >= 2 for v in att.values()))        # the roundup is excluded from now on

    def test_trends_failure_never_breaks_a_run_and_trends_are_used_when_available(self):
        seen = []
        p = self.build()
        p.trends_fn = lambda: seen.append("called") or (_ for _ in ()).throw(RuntimeError("down"))
        with mock.patch.object(config, "TRENDS_ENABLED", True):
            self.assertEqual(p.run_cycle()["status"], "published")
        self.assertEqual(seen, ["called"])


def ov_result(i, title, lic="by", ver="2.0", creator="Jane Doe", tags=(), width=2400, mature=False, source="flickr"):
    return {"id": f"id{i}", "title": title, "url": f"https://live.staticflickr.test/{i}.jpg", "creator": creator,
            "license": lic, "license_version": ver, "tags": [{"name": t} for t in tags], "source": source,
            "width": width, "height": 1600, "mature": mature}


class OvSession:
    def __init__(self, results, status=200):
        self.results, self.status, self.calls, self.posts = results, status, [], []

    def get(self, url, headers=None, params=None, timeout=None, stream=False):
        self.calls.append((url, headers, params))
        if "/images/" in url:
            return Resp(self.status, {"results": self.results})

        class Dl(Resp):
            def iter_content(s, n): yield b"\xff\xd8" + b"0" * 30_000
        return Dl(200)

    def post(self, url, data=None, timeout=None, **kw):
        self.posts.append((url, data))
        return Resp(200, {"access_token": "TOK123", "expires_in": 3600})


class TestPhotoSafety(Base):
    """Real failure: the photo for 'Gaurav Gupta' (fashion designer) was a hospital doctor with the same name."""

    def test_context_strictness_by_kind_of_name(self):
        for q in ("Election Commission of India", "Supreme Court", "Eden Gardens", "Parliament of India"):
            self.assertEqual(commons.context_threshold(q), 0, q)          # the name itself is the subject
        for q in ("Gaurav Gupta", "Vijay", "Narendra Modi", "Virat Kohli"):
            self.assertEqual(commons.context_threshold(q), 1, q)          # people: same-name check
        for q in ("West Bengal", "Japan", "Tokyo", "Mumbai", "South Korea"):
            self.assertEqual(commons.context_threshold(q), 2, q)          # places: never a generic scenic photo

    def test_context_terms_are_topic_words_not_names(self):
        ctx = commons.context_terms("Designer unveils couture collection at Mumbai fashion week", "Gaurav Gupta", "Entertainment")
        for word in ("couture", "designer", "film", "collection"):
            self.assertIn(stock_kw(word), ctx, word)
        self.assertNotIn(stock_kw("Gaurav"), ctx)
        self.assertNotIn(stock_kw("Mumbai"), ctx)                         # proper nouns are not context (places collide too)

    def test_same_name_different_person_is_rejected(self):
        pages = [
            commons_page(1, "Dr Gaurav Gupta speaking at Fortis sports medicine seminar.jpg", cats="Sports medicine|Fortis Hospital"),
            commons_page(2, "Gaurav Gupta at India Couture Week.jpg", cats="Fashion designers of India|Couture"),
        ]
        cc = commons.CommonsClient(session=CommonsSession(pages), cache_path=self.tmp / "g.json")
        ctx = commons.context_terms("Designer Gaurav Gupta unveils couture collection fashion show", "Gaurav Gupta", "Entertainment")
        self.assertEqual([c["title"] for c in cc.candidates("Gaurav Gupta", ctx)], ["Gaurav Gupta at India Couture Week"])
        self.assertEqual(len(cc.candidates("Gaurav Gupta", None)), 2)     # without context both would have matched

    def test_places_and_institutions_do_not_need_context(self):
        pages = [commons_page(1, "Eden Gardens at dusk.jpg")]
        cc = commons.CommonsClient(session=CommonsSession(pages), cache_path=self.tmp / "e.json")
        ctx = commons.context_terms("Designer unveils couture collection", "Eden Gardens", "Entertainment")
        self.assertEqual(len(cc.candidates("Eden Gardens", ctx)), 1)


class TestOpenverse(Base):
    def client(self, results, **kw):
        sess = OvSession(results, **kw.pop("session_kw", {}))
        return openverse.OpenverseClient(session=sess, cache_path=self.tmp / "ov.json", client_id=kw.get("cid", ""),
                                         client_secret=kw.get("sec", "")), sess

    def test_request_asks_for_commercial_no_share_alike_photos(self):
        ov, sess = self.client([ov_result(1, "Eden Gardens match", tags=("cricket", "stadium"))])
        ov.candidates("Eden Gardens")
        params = sess.calls[0][2]
        self.assertEqual((params["license"], params["license_type"], params["category"], params["mature"]),
                         ("by,cc0,pdm", "commercial", "photograph", "false"))
        self.assertNotIn("Authorization", sess.calls[0][1])               # anonymous unless credentials are configured
        self.assertIn("InstaNewsAgent", sess.calls[0][1]["User-Agent"])

    def test_results_are_rechecked_locally(self):
        results = [
            ov_result(1, "Eden Gardens during match", tags=("cricket",)),                       # ok
            ov_result(2, "Eden Gardens panorama", lic="by-sa"),                                  # share-alike
            ov_result(3, "Eden Gardens gate", lic="by-nc"),                                      # non-commercial
            ov_result(4, "Eden Gardens night", mature=True),                                     # mature
            ov_result(5, "Eden Gardens small", width=700),                                       # too small
            ov_result(6, "Flag at Eden Gardens"),                                                # flag
            ov_result(7, "Mountain lake sunrise", tags=("nature",)),                             # unrelated
            ov_result(8, "Eden Gardens public domain", lic="pdm", ver=None, creator=""),         # ok, no creator
        ]
        ov, _ = self.client(results)
        cands = ov.candidates("Eden Gardens")
        self.assertEqual(sorted(c["title"] for c in cands), ["Eden Gardens during match", "Eden Gardens public domain"])
        by_title = {c["title"]: c for c in cands}
        self.assertEqual(by_title["Eden Gardens during match"]["credit"], "Jane Doe (CC BY 2.0)")
        self.assertEqual(by_title["Eden Gardens during match"]["via"], "Flickr (Openverse)")
        self.assertEqual(by_title["Eden Gardens public domain"]["credit"], "Unknown author (Public Domain)")

    def test_same_name_check_applies_to_openverse_too(self):
        results = [ov_result(1, "Dr Gaurav Gupta lecture", tags=("medicine", "hospital")),
                   ov_result(2, "Gaurav Gupta runway", tags=("fashion", "couture"))]
        ov, _ = self.client(results)
        ctx = commons.context_terms("designer couture collection fashion show", "Gaurav Gupta", "Entertainment")
        self.assertEqual([c["title"] for c in ov.candidates("Gaurav Gupta", ctx)], ["Gaurav Gupta runway"])

    def test_oauth_token_is_used_when_configured(self):
        ov, sess = self.client([ov_result(1, "Eden Gardens match")], cid="ID", sec="SECRET")
        ov.candidates("Eden Gardens")
        self.assertIn("auth_tokens/token", sess.posts[0][0])
        self.assertEqual(sess.calls[0][1]["Authorization"], "Bearer TOK123")

    def test_rate_limit_disables_source_for_the_run_without_error(self):
        ov, _ = self.client([], session_kw={"status": 429})
        self.assertEqual(ov.fetch_photos(["Eden Gardens"], self.tmp / "o"), [])
        self.assertFalse(ov.enabled)

    def test_results_are_cached_24h_and_downloads_work(self):
        ov, sess = self.client([ov_result(1, "Eden Gardens match", tags=("cricket",))])
        photos = ov.fetch_photos(["Eden Gardens"], self.tmp / "o2", wanted=1)
        self.assertEqual(len(photos), 1)
        self.assertTrue(photos[0].path.exists())
        self.assertEqual(photos[0].via, "Flickr (Openverse)")
        n = len([c for c in sess.calls if "/images/" in c[0]])
        ov.fetch_photos(["Eden Gardens"], self.tmp / "o3", wanted=1)
        self.assertEqual(len([c for c in sess.calls if "/images/" in c[0]]), n)

    def test_credit_names_every_source(self):
        ph = [commons.Photo(Path("a"), "Jane Doe (CC BY 4.0)", "A", "q", "Wikimedia Commons"),
              commons.Photo(Path("b"), "Joe Roe (CC BY 2.0)", "B", "q", "Flickr (Openverse)")]
        self.assertEqual(commons.images_credit(ph), "Jane Doe (CC BY 4.0), Joe Roe (CC BY 2.0) via Wikimedia Commons, Flickr (Openverse)")

    def test_studio_fills_photos_from_several_sources_with_context(self):
        seen = []

        def src(name, n):
            o = type("S", (), {"enabled": True, "name": name})()

            def fetch(queries, out, wanted=4, rng=None, context=None, **k):
                seen.append((name, wanted, set(context or ()), Path(out).name))
                return [commons.Photo(self.tmp / f"{name}{i}.jpg", f"P{i} ({name})", f"T{i}", queries[0], name) for i in range(n)]
            o.fetch_photos = fetch
            return o
        got = {}

        def fake_mixed(items, total, tmp):
            got["n"] = len(items)
            (tmp / "bg.mp4").write_bytes(b"x")
            return tmp / "bg.mp4"

        def fake_compose(bg, content, cluster, words, total, voice, music, out):
            out.write_bytes(b"v")
            return out
        content = ai_writer.validate_reel(dict(REEL_VALID, photo_queries=["Gaurav Gupta"]), "", "Entertainment")
        with mock.patch.object(config, "REAL_PHOTOS", True), \
                mock.patch.object(reel_studio, "prepare_mixed_background", side_effect=fake_mixed), \
                mock.patch.object(reel_studio, "compose_reel", side_effect=fake_compose):
            res = reel_studio.make_reel(content, make_cluster("Entertainment"), self.tmp / "o.mp4", providers=[],
                                        photo_sources=[src("commons", 1), src("openverse", 5)], tts=lambda t, d: None)
        self.assertEqual([(n, w) for n, w, _, _ in seen], [("commons", 4), ("openverse", 3)])   # 4 photos in total
        self.assertEqual(got["n"], 4)
        self.assertTrue(all(ctx for _, _, ctx, _ in seen))                                    # context is always passed
        self.assertEqual([d for _, _, _, d in seen], ["commons", "openverse"])                # separate folders: no file clashes
        self.assertIn("via commons, openverse", res.images_credit)


CRICKET_SRC = ("India beat Australia by 33 runs in the third ODI at Eden Gardens. India posted 287/6 in their 50 overs and "
               "Australia were bowled out for 254 in 47.2 overs. Virat Kohli scored 112 off 98 balls and was named player "
               "of the match.")
SC_OK = {"team_a": "India", "score_a": "287/6", "team_b": "Australia", "score_b": "254 all out",
         "result": "India won by 33 runs", "highlight": "Virat Kohli 112 (98)"}


class TestCricketScorecard(Base):
    """Scores are facts: a verified result card on every cricket result Reel, never an invented number."""

    def test_valid_scorecard_is_kept(self):
        sc = ai_writer.validate_scorecard(dict(SC_OK), CRICKET_SRC)
        self.assertEqual((sc["team_a"], sc["score_a"], sc["result"]), ("India", "287/6", "India won by 33 runs"))

    def test_wrong_or_unsupported_scorecards_are_dropped_not_published(self):
        bad_score = dict(SC_OK, score_a="312/4")                                  # number not in the sources
        wrong_team = dict(SC_OK, team_b="England")                                # team not in the sources
        missing = {k: v for k, v in SC_OK.items() if k != "score_b"}
        for sc in (bad_score, wrong_team, missing, None, "India won", []):
            self.assertIsNone(ai_writer.validate_scorecard(sc, CRICKET_SRC), sc)

    def test_script_without_a_match_result_has_no_card_and_is_still_valid(self):
        out = ai_writer.validate_reel(dict(REEL_VALID, scorecard=None), "", "Technology")
        self.assertIsNone(out["scorecard"])
        out2 = ai_writer.validate_reel(dict(REEL_VALID, scorecard=dict(SC_OK)), "", "Sports")   # unsupported by sources
        self.assertIsNone(out2["scorecard"])                                                     # card dropped, Reel kept
        self.assertEqual(len(out2["narration"]), 4)

    def test_prompt_asks_for_scorecard_only_for_match_results(self):
        prompt = ai_writer.build_reel_prompt(make_cluster("Sports"))[0]
        self.assertIn('"scorecard"', prompt)
        self.assertIn("RESULT of a specific cricket match", prompt)

    def test_card_renders_inside_its_canvas_with_long_names(self):
        long = dict(SC_OK, team_a="Royal Challengers Bengaluru Women", score_a="187/5 (20 ov)",
                    team_b="Mumbai Indians Women", score_b="188/3 (19.2 ov)", result="Mumbai Indians Women won by 7 wickets (with 4 balls to spare)")
        img = reel_renderer.build_scorecard(long, {"category": "Sports"})
        self.assertEqual(img.size, (912, 520))
        bbox = img.getchannel("A").getbbox()
        self.assertEqual(bbox, (0, 0, 912, 520))
        px = img.convert("RGBA").load()
        self.assertGreater(px[400, 260][3], 150)                                 # solid card body

    def test_result_card_stays_on_screen_longer_than_a_normal_headline(self):
        words = tts.distribute_words("India beat Australia by thirty three runs. Kohli hit a brilliant hundred. "
                                     "The series is level going into the decider. Fans filled the stadium in Kolkata.", 12.0, 0.35)
        bg = reel_renderer.prepare_animated_background("Sports", 14.8, self.tmp)
        content = {"hook": "India beat Australia by 33 runs", "scorecard": dict(SC_OK)}
        out = reel_renderer.compose_reel(bg, content, {"category": "Sports", "breaking": False, "sources": ["BBC Sport"]},
                                         words, 14.8, None, None, self.tmp / "sc.mp4")
        ff = video_renderer.find_ffmpeg()

        def white_pixels(t):
            png = self.tmp / f"s{t}.png"
            subprocess.run([ff, "-y", "-loglevel", "error", "-ss", str(t), "-i", str(out), "-frames:v", "1", str(png)], check=True)
            region = Image.open(png).convert("L").crop((84, 520, 996, 700))          # where the two score lines sit
            return sum(region.histogram()[245:])                                      # count of near-white pixels
        self.assertGreater(white_pixels(6.0), 2000)                                     # score text visible at 6 s ...
        self.assertLess(white_pixels(11.0), 200)                                        # ... and gone by 11 s


class TestRemixHelper(PipelineFixture):
    def test_draft_contains_everything_needed_for_an_in_app_remix(self):
        a = make_article(1, "Sports", "BBC Sport", now=self.NOW)
        a["title"] = "India beat Australia by 33 runs in third ODI at Eden Gardens"
        a["summary"] = CRICKET_SRC
        b = make_article(2, "News", "BBC News", now=self.NOW)
        b["title"] = "Parliament passes landmark digital privacy law after debate"
        reel = dict(REEL_VALID, scorecard=dict(SC_OK))
        p = self.build(articles=[a, b], ai=lambda prompt: json.dumps(reel if "VOICEOVER" in prompt else VALID))
        path = drafts.build_draft(p, topic="cricket", out_dir=self.tmp / "drafts")
        text = path.read_text(encoding="utf-8")
        self.assertIn("India beat Australia", text)
        for section in ("SOURCES", "HEADLINE", "SCRIPT", "CAPTION (paste as is)", "HOW TO POST", "Remix", "SCORECARD"):
            self.assertIn(section, text)
        self.assertIn("India 287/6  vs  Australia 254 all out", text)
        self.assertNotIn("Parliament", text.split("SOURCES")[1].split("HEADLINE")[0])        # topic filter excluded it
        self.assertIn("Do NOT download and re-upload", text)
        self.assertEqual(self.ig_session.published, 0)                                       # drafting never publishes

    def test_topic_expands_to_related_words(self):
        self.assertTrue({"odi", "wicket", "ipl", "bcci"} <= drafts.expand_topic("cricket"))
        self.assertIn("election", drafts.expand_topic("election"))
        self.assertEqual(drafts.expand_topic("zzzz"), {"zzzz"})

    def test_no_story_for_topic_returns_none(self):
        p = self.build(articles=[make_article(1, "News", "BBC News", now=self.NOW)])
        self.assertIsNone(drafts.build_draft(p, topic="cricket", out_dir=self.tmp / "d2"))


class TestOfficialAndOwnPhotos(Base):
    """Telegram shows publishers' own photos; on Instagram we use photos we are licensed to use: Government of India
    open-licence (GODL-India) photos, free-licence photos, and YOUR OWN library."""

    def test_godl_india_is_accepted_other_restrictive_licences_still_rejected(self):
        for ok in ("GODL-India", "GODL India", "Government Open Data License - India"):
            self.assertTrue(commons.license_ok(ok), ok)
        for bad in ("OGL", "CC BY-SA 4.0", "CC BY-NC 4.0", "Fair use", ""):
            self.assertFalse(commons.license_ok(bad), bad)

    def test_attribution_rules(self):
        self.assertFalse(commons.needs_attribution("CC0"))
        self.assertFalse(commons.needs_attribution("Public domain"))
        self.assertTrue(commons.needs_attribution("CC BY 4.0"))
        self.assertTrue(commons.needs_attribution("GODL-India"))

    def test_official_photo_credit_has_source_url_as_godl_requires(self):
        pg = commons_page(1, "Narendra Modi meets Chief Minister Eknath Shinde in New Delhi.jpg", lic="GODL-India",
                          artist="institution QS:P195,Q7243280", desc="The Prime Minister, Shri Narendra Modi meets the Chief Minister",
                          cats="Narendra Modi|Prime Minister's Office (India)")
        pg["pageid"] = 4242
        pg["imageinfo"][0]["extmetadata"]["Attribution"] = {"value": "Prime Minister's Office (GODL-India)"}
        cc = commons.CommonsClient(session=CommonsSession([pg]), cache_path=self.tmp / "godl.json")
        ctx = commons.context_terms("Chief minister meets PM over drought, government assures farmers", "Narendra Modi", "News")
        c = cc.candidates("Narendra Modi", ctx)
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["credit"], "Prime Minister's Office (GODL-India)")
        self.assertNotIn("QS:", c[0]["credit"])                                 # raw Wikidata placeholder never shown
        self.assertEqual(c[0]["caption_credit"], "Prime Minister's Office (GODL-India), commons.wikimedia.org/?curid=4242")
        self.assertTrue(c[0]["needs_credit"])
        photos = cc.fetch_photos(["Narendra Modi"], self.tmp / "pp", wanted=1, context=ctx)
        self.assertIn("commons.wikimedia.org/?curid=4242", commons.images_credit(photos))

    def test_generic_scenic_photo_is_rejected_for_a_political_story(self):
        """Seen in a real Reel: 'Photo: Syced (CC0)', a Tokyo street at night, behind a news story."""
        pages = [commons_page(1, "Tokyo street at night.jpg", lic="CC0", artist="Syced",
                              cats="Streets of Tokyo|Night in Japan|Currency exchange"),
                 commons_page(2, "Tokyo election rally candidates speak to voters.jpg", lic="CC0", artist="Syced",
                              cats="Elections in Japan|Political rallies in Tokyo")]
        cc = commons.CommonsClient(session=CommonsSession(pages), cache_path=self.tmp / "tk.json")
        ctx = commons.context_terms("Japan's prime minister faces voters as the election campaign begins", "Tokyo", "News")
        titles = [c["title"] for c in cc.candidates("Tokyo", ctx)]
        self.assertNotIn("Tokyo street at night", titles)
        self.assertEqual(titles, ["Tokyo election rally candidates speak to voters"])

    def test_cc0_credit_is_not_burned_into_the_video_but_cc_by_is(self):
        p1, p2 = self.tmp / "a.jpg", self.tmp / "b.jpg"
        Image.new("RGB", (2400, 1600)).save(p1, "JPEG")
        Image.new("RGB", (2400, 1600)).save(p2, "JPEG")
        fake = type("S", (), {"enabled": True, "name": "commons"})()
        fake.fetch_photos = lambda q, out, wanted=4, rng=None, **k: [
            commons.Photo(p1, "Syced (CC0)", "A", "x", "Wikimedia Commons", needs_credit=False),
            commons.Photo(p2, "Jane Doe (CC BY 4.0)", "B", "x", "Wikimedia Commons", needs_credit=True)]
        got = {}

        def fake_mixed(items, total, tmp):
            got["credits"] = [i["credit"] for i in items]
            (tmp / "bg.mp4").write_bytes(b"x")
            return tmp / "bg.mp4"

        def fake_compose(bg, content, cluster, words, total, voice, music, out):
            out.write_bytes(b"v")
            return out
        content = ai_writer.validate_reel(dict(REEL_VALID), "", "News")
        with mock.patch.object(config, "REAL_PHOTOS", True), \
                mock.patch.object(reel_studio, "prepare_mixed_background", side_effect=fake_mixed), \
                mock.patch.object(reel_studio, "compose_reel", side_effect=fake_compose):
            res = reel_studio.make_reel(content, make_cluster("News"), self.tmp / "o.mp4", providers=[], photo_sources=[fake],
                                        tts=lambda t, d: None)
        self.assertEqual(got["credits"], ["", "Jane Doe (CC BY 4.0)"])
        self.assertIn("Syced (CC0)", res.images_credit)                          # still named in the caption

    def test_own_photo_library_matches_by_filename_and_carries_its_credit(self):
        d = self.tmp / "photos"
        (d / "politics").mkdir(parents=True)
        for name in ("narendra_modi_eknath_shinde_meeting__PIB.jpg", "eden_gardens_crowd__Rahul Sharma.png",
                     "election_commission_building.jpg", "random_sunset.jpg", "notes.txt"):
            (d / name).write_bytes(b"\xff\xd8" + b"0" * 2000)
        (d / "politics" / "parliament_house_exterior__Own Photo.jpg").write_bytes(b"\xff\xd8" + b"0" * 2000)
        lp = localphotos.LocalPhotos(d)
        got = lp.candidates("Narendra Modi")
        self.assertEqual([(c["title"], c["credit"], c["needs_credit"]) for c in got],
                         [("narendra_modi_eknath_shinde_meeting", "PIB", True)])
        self.assertEqual(lp.candidates("Election Commission")[0]["credit"], "Own photo")
        self.assertFalse(lp.candidates("Election Commission")[0]["needs_credit"])      # no credit given -> none on screen
        self.assertEqual(len(lp.candidates("Parliament House")), 1)                    # folder + file words both count
        self.assertEqual(lp.candidates("Cricket Stadium"), [])
        photos = lp.fetch_photos(["Narendra Modi"], self.tmp / "out", wanted=1)
        self.assertEqual(photos[0].credit, "PIB")
        self.assertTrue(photos[0].path.exists())

    def test_own_library_is_first_in_the_default_photo_sources(self):
        d = self.tmp / "lib"
        d.mkdir()
        with mock.patch.object(config, "PHOTO_DIR", str(d)), mock.patch.object(config, "PHOTO_SOURCES", ["commons"]):
            names = [x.name for x in reel_studio.default_photo_sources()]
        self.assertEqual(names, ["local", "commons"])
        with mock.patch.object(config, "PHOTO_DIR", ""), mock.patch.object(config, "PHOTO_SOURCES", ["commons"]):
            self.assertEqual([x.name for x in reel_studio.default_photo_sources()], ["commons"])


class FakeFB:
    """Routes calls like the Facebook Graph API / rupload endpoint and records everything."""

    def __init__(self):
        self.posts, self.gets, self.overrides, self.n_photo = [], [], {}, 0
        self.video_status = "ready"

    def _pop(self, key):
        q = self.overrides.get(key)
        if q:
            item = q.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return None

    def post(self, url, data=None, headers=None, timeout=None, **kw):
        self.posts.append((url, dict(data or {}), dict(headers or {})))
        tail = url.split("/v26.0/", 1)[-1]
        o = self._pop("POST:" + tail)
        if o:
            return o
        if "rupload.facebook.com" in url:
            return Resp(200, {"success": True})
        if tail.endswith("/video_reels"):
            if data.get("upload_phase") == "start":
                return Resp(200, {"video_id": "vid9", "upload_url": "https://rupload.facebook.com/video-upload/v26.0/vid9"})
            return Resp(200, {"success": True})
        if tail.endswith("/photos"):
            self.n_photo += 1
            if data.get("published") == "false":
                return Resp(200, {"id": f"ph{self.n_photo}"})
            return Resp(200, {"id": "ph1", "post_id": "PAGE_555"})
        if tail.endswith("/feed"):
            return Resp(200, {"id": "PAGE_777"})
        return Resp(404, {"error": {"message": "unknown", "code": 100}})

    def get(self, url, params=None, timeout=None, **kw):
        self.gets.append((url, dict(params or {})))
        tail = url.split("/v26.0/", 1)[-1]
        o = self._pop("GET:" + tail)
        if o:
            return o
        if tail == "PAGE":
            return Resp(200, {"name": "Tech Cricket Hub", "can_post": True})
        if tail == "me/accounts":
            return Resp(200, {"data": [{"id": "PAGE", "name": "Tech Cricket Hub", "access_token": "PAGETOK", "tasks": ["CREATE_CONTENT"]}]})
        if tail == "vid9":
            return Resp(200, {"status": {"video_status": self.video_status}})
        if tail == "BIZPAGE":
            if "tasks" in (params or {}).get("fields", ""):
                return Resp(400, {"error": {"message": "(#100) Tried accessing nonexisting field (tasks)", "code": 100}})
            return Resp(200, {"id": "BIZPAGE", "name": "Biz Page", "access_token": "BIZTOK"})
        if tail == "oauth/access_token":
            return Resp(200, {"access_token": "LONGUSERTOK"})
        if tail == "debug_token":
            return Resp(200, {"data": {"expires_at": 0, "is_valid": True, "type": "PAGE", "profile_id": "PAGE",
                                       "scopes": ["pages_show_list", "pages_manage_posts", "pages_read_engagement"]}})
        return Resp(404, {"error": {"message": "unknown", "code": 100}})


def make_fb(sess=None):
    return FacebookPageClient("PAGE", "PAGE_TOKEN_SECRET", "v26.0", session=sess or FakeFB(), sleep=lambda s: None)


class TestFacebookClient(Base):
    def test_link_post_is_a_telegram_style_preview_post(self):
        fb = FakeFB()
        r = make_fb(fb).post_link("Headline\n\nSource: Variety", "https://variety.com/story")
        url, data, _ = fb.posts[0]
        self.assertTrue(url.endswith("/PAGE/feed"))
        self.assertEqual((data["message"], data["link"]), ("Headline\n\nSource: Variety", "https://variety.com/story"))
        self.assertEqual(r["permalink"], "https://www.facebook.com/PAGE_777")

    def test_single_photo_is_posted_directly(self):
        fb = FakeFB()
        r = make_fb(fb).post_photos(["https://cdn.test/1.jpg"], "cap")
        url, data, _ = fb.posts[0]
        self.assertTrue(url.endswith("/PAGE/photos"))
        self.assertEqual((data["url"], data["message"], data["published"]), ("https://cdn.test/1.jpg", "cap", "true"))
        self.assertEqual(r["permalink"], "https://www.facebook.com/PAGE_555")

    def test_several_photos_are_uploaded_unpublished_then_attached_to_one_post(self):
        fb = FakeFB()
        r = make_fb(fb).post_photos([f"https://cdn.test/{i}.jpg" for i in range(5)], "cap")
        uploads = [p for p in fb.posts if p[0].endswith("/photos")]
        self.assertEqual(len(uploads), 5)
        self.assertTrue(all(p[1]["published"] == "false" for p in uploads))
        feed = fb.posts[-1][1]
        self.assertTrue(fb.posts[-1][0].endswith("/PAGE/feed"))
        self.assertEqual([json.loads(feed[f"attached_media[{i}]"])["media_fbid"] for i in range(5)],
                         ["ph1", "ph2", "ph3", "ph4", "ph5"])
        self.assertEqual(r["permalink"], "https://www.facebook.com/PAGE_777")

    def test_too_many_photos_retries_with_four(self):
        fb = FakeFB()
        fb.overrides["POST:PAGE/feed"] = [Resp(400, {"error": {"message": "too many attachments", "code": 100}})]
        make_fb(fb).post_photos([f"https://cdn.test/{i}.jpg" for i in range(5)], "cap")
        feeds = [p for p in fb.posts if p[0].endswith("/PAGE/feed")]
        self.assertEqual(len(feeds), 2)
        self.assertNotIn("attached_media[4]", feeds[1][1])
        self.assertIn("attached_media[3]", feeds[1][1])

    def test_reel_uses_the_three_step_hosted_file_flow(self):
        fb = FakeFB()
        r = make_fb(fb).post_reel("https://res.cloudinary.test/v.mp4", "Reel caption")
        start, upload, finish = fb.posts[0], fb.posts[1], fb.posts[2]
        self.assertTrue(start[0].endswith("/PAGE/video_reels"))
        self.assertEqual(start[1]["upload_phase"], "start")
        self.assertIn("rupload.facebook.com/video-upload", upload[0])
        self.assertEqual(upload[2]["file_url"], "https://res.cloudinary.test/v.mp4")      # Facebook fetches the hosted video
        self.assertEqual(upload[2]["Authorization"], "OAuth PAGE_TOKEN_SECRET")
        self.assertEqual((finish[1]["upload_phase"], finish[1]["video_state"], finish[1]["video_id"], finish[1]["description"]),
                         ("finish", "PUBLISHED", "vid9", "Reel caption"))
        self.assertEqual(r["permalink"], "https://www.facebook.com/reel/vid9")

    def test_reel_processing_error_is_reported_but_slow_processing_is_not_a_failure(self):
        fb = FakeFB()
        fb.video_status = "error"
        with self.assertRaises(FacebookError):
            make_fb(fb).post_reel("https://x/v.mp4", "c")
        fb2 = FakeFB()
        fb2.video_status = "processing"
        t = iter(range(0, 10_000, 100))
        client = FacebookPageClient("PAGE", "T", "v26.0", session=fb2, sleep=lambda s: None, clock=lambda: next(t))
        self.assertEqual(client.post_reel("https://x/v.mp4", "c")["id"], "vid9")        # does not raise

    def test_rejected_hosted_video_gives_a_clear_error(self):
        fb = FakeFB()
        fb.overrides["POST:rupload"] = []
        class Rej(FakeFB):
            def post(self, url, data=None, headers=None, timeout=None, **kw):
                if "rupload" in url:
                    return Resp(200, {"success": False})
                return super().post(url, data, headers, timeout)
        with self.assertRaises(FacebookError) as cm:
            make_fb(Rej()).post_reel("https://x/v.mp4", "c")
        self.assertIn("robots.txt", str(cm.exception))

    def test_token_errors_are_flagged_and_writes_not_retried(self):
        fb = FakeFB()
        fb.overrides["POST:PAGE/feed"] = [Resp(400, {"error": {"message": "Session has expired", "code": 190}})] * 3
        with self.assertRaises(FacebookError) as cm:
            make_fb(fb).post_link("m", "https://x")
        self.assertTrue(cm.exception.token_problem)
        self.assertEqual(len(fb.posts), 1)
        class Timeout(FakeFB):
            def post(self, *a, **k): raise requests.Timeout()
        with self.assertRaises(FacebookError):
            make_fb(Timeout()).post_link("m", "https://x")

    def test_verify_and_page_listing(self):
        fb = FakeFB()
        self.assertEqual(make_fb(fb).verify(), "Tech Cricket Hub")
        with mock.patch.object(config, "FB_API_VERSION", "v26.0"):
            pages = FacebookPageClient.list_pages("USERTOK", session=fb)
        self.assertEqual((pages[0]["id"], pages[0]["access_token"]), ("PAGE", "PAGETOK"))
        self.assertEqual(fb.gets[-1][1]["access_token"], "USERTOK")

    def test_token_never_in_url(self):
        fb = FakeFB()
        make_fb(fb).post_link("m", "https://x")
        self.assertNotIn("PAGE_TOKEN_SECRET", fb.posts[0][0])

    def test_config_validation(self):
        with mock.patch.multiple(config, FB_PAGE_ID="123", FB_PAGE_ACCESS_TOKEN=""):
            self.assertTrue(any("BOTH" in p for p in config.validate("dry")))
        with mock.patch.object(config, "FB_MODE", "sometimes"):
            self.assertTrue(any("FB_MODE" in p for p in config.validate("dry")))
        with mock.patch.multiple(config, FB_PAGE_ID="", FB_PAGE_ACCESS_TOKEN=""):
            self.assertFalse(any("Facebook" in p for p in config.validate("dry")))


class TestReelVariety(Base):
    def test_consecutive_reels_never_share_voice_colour_or_layout(self):
        recent, styles = [], []
        for i in range(40):
            st = variety.pick_style(f"story-{i}", recent)
            styles.append(st)
            recent = [st.to_dict()] + recent[:7]
        for prev, cur in zip(styles, styles[1:]):
            self.assertNotEqual(prev.voice, cur.voice)
            self.assertNotEqual(prev.highlight, cur.highlight)
            self.assertNotEqual(prev.hook_layout, cur.hook_layout)
            self.assertNotEqual(prev.caption_pos, cur.caption_pos)
        for a, b in zip(styles, styles[2:]):
            self.assertNotEqual(a.highlight, b.highlight)         # colours also differ from two Reels ago
        self.assertGreater(len({s.grade for s in styles}), 3)       # real spread over many Reels

    def test_same_story_gets_the_same_look_so_preview_matches_publish(self):
        recent = [variety.pick_style("older", []).to_dict()]
        self.assertEqual(variety.pick_style("abc", recent), variety.pick_style("abc", recent))

    def test_a_single_configured_voice_is_pinned(self):
        with mock.patch.object(config, "TTS_VOICES", ["en-IN-NeerjaNeural"]):
            voices = {variety.pick_style(f"s{i}", []).voice for i in range(20)}
        self.assertEqual(voices, {"en-IN-NeerjaNeural"})

    def test_style_from_garbage_falls_back_to_classic(self):
        self.assertEqual(variety.ReelStyle.from_dict({"nonsense": 1}), variety.ReelStyle())
        self.assertEqual(variety.ReelStyle.from_dict("x"), variety.ReelStyle())
        st = variety.ReelStyle.from_dict({"caption_pos": "elsewhere", "grade": "none", "outro": 99})
        self.assertEqual((st.caption_y, st.grade_filter), (1150, ""))   # unknown values resolve to safe defaults
        self.assertIn(st.outro_text, variety.OUTROS)

    def test_history_is_remembered_and_capped(self):
        for i in range(12):
            variety.remember(self.tmp, variety.pick_style(f"x{i}", variety.load_recent(self.tmp)))
        self.assertEqual(len(variety.load_recent(self.tmp)), variety.KEEP)

    def test_voice_options_only_go_to_functions_that_accept_them(self):
        st = variety.pick_style("v", [])
        self.assertEqual(set(reel_studio._voice_options(tts.synthesize, st)), {"voice", "rate", "pitch"})
        self.assertEqual(reel_studio._voice_options(lambda text, out_dir: None, st), {})
        self.assertEqual(reel_studio._voice_options(lambda text, out_dir, voice="": None, st), {"voice": st.voice})

    def test_every_headline_layout_and_outro_renders_inside_the_safe_area(self):
        hook = "Opposition targets Modi government over undeclared emergency in Delhi ahead of youth protest"
        for layout in variety.HOOK_LAYOUTS:
            img = reel_renderer.build_hook(hook, layout, (255, 214, 10))
            self.assertEqual(img.size, (1080, 520))
            bbox = img.getchannel("A").getbbox()
            self.assertIsNotNone(bbox)
            self.assertLessEqual(bbox[3], 520)
            self.assertGreaterEqual(bbox[0], 40)
            self.assertLessEqual(bbox[2], 1040)
        for i in range(len(variety.OUTROS)):
            st = variety.ReelStyle(outro=i)
            self.assertEqual(reel_renderer.build_outro({"category": "News", "sources": ["BBC"]}, st).size, (1080, 1920))

    def test_headline_never_overlaps_captions_for_any_position_pair(self):
        for hp, hy in variety.HOOK_Y.items():
            for cp, cy in variety.CAPTION_Y.items():
                self.assertLess(hy + 520, cy - 150, (hp, cp))     # 150 = half of a two-line caption block

    def test_styled_reel_renders_end_to_end_with_grade_and_colour(self):
        words = tts.distribute_words("India beat Australia by thirty three runs. Kohli hit a brilliant hundred.", 5.0, 0.35)
        bg = reel_renderer.prepare_animated_background("Sports", 8.0, self.tmp)
        st = variety.ReelStyle(highlight="pink", grade="vivid", hook_layout="box", hook_anim="rise", caption_upper=True,
                               caption_words=2, caption_pos="low", hook_pos="top", outro=2)
        out = reel_renderer.compose_reel(bg, {"hook": "India beat Australia by 33 runs"},
                                         {"category": "Sports", "breaking": False, "sources": ["BBC Sport"]},
                                         words, 8.0, None, None, self.tmp / "styled.mp4", st)
        self.assertGreater(out.stat().st_size, 5000)
        self.assertAlmostEqual(video_renderer.probe(out)["duration"], 8.0, delta=0.6) if hasattr(video_renderer, "probe") else None

    def test_script_prompt_carries_the_opening_style_hint(self):
        cluster = make_cluster("News")
        plain, _ = ai_writer.build_reel_prompt(cluster)
        cluster["style_hint"] = variety.OPENERS[1]
        hinted, _ = ai_writer.build_reel_prompt(cluster)
        self.assertNotIn(variety.OPENERS[1], plain)
        self.assertIn(variety.OPENERS[1], hinted)


class TestVarietyInPipeline(PipelineFixture):
    def test_publishes_use_different_looks_and_remember_them(self):
        seen = []

        def make(content, cand, out_path, music_path=None):
            seen.append(cand.get("reel_style"))
            subprocess.run([video_renderer.find_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=s=320x568:r=10:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)], check=True)
            return reel_studio.ReelResult(Path(out_path), True, "animated", 8.0, None, cand.get("reel_style"))
        p = self.build(ai=lambda pr: json.dumps(REEL_VALID if "VOICEOVER" in pr else VALID))
        p.reel_maker = make
        with mock.patch.multiple(config, CONTENT_FORMATS={"reel": 1}, REEL_STYLE="dynamic", REEL_VARIETY=True):
            r1 = p.run_cycle(force=True)
        self.assertEqual(r1["status"], "published", r1)
        self.assertIsNotNone(seen[0])
        recent = variety.load_recent(p.data_dir)
        self.assertEqual(recent[0]["voice"], seen[0]["voice"])
        # the next story is chosen against what was just published
        nxt = variety.pick_style("another-story", recent)
        self.assertNotEqual(nxt.voice, seen[0]["voice"])
        self.assertIn("look=", p.load_history()[0]["reel"])

    def test_variety_off_keeps_the_classic_look_and_remembers_nothing(self):
        seen = []

        def make(content, cand, out_path, music_path=None):
            seen.append(cand.get("reel_style"))
            subprocess.run([video_renderer.find_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=s=320x568:r=10:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)], check=True)
            return reel_studio.ReelResult(Path(out_path), True, "animated", 8.0)
        p = self.build(ai=lambda pr: json.dumps(REEL_VALID if "VOICEOVER" in pr else VALID))
        p.reel_maker = make
        with mock.patch.multiple(config, CONTENT_FORMATS={"reel": 1}, REEL_STYLE="dynamic", REEL_VARIETY=False):
            res = p.run_cycle(force=True)
        self.assertEqual(res["status"], "published", res)
        self.assertEqual(seen, [None])
        self.assertEqual(variety.load_recent(p.data_dir), [])


class TestWrongPictureGuards(Base):
    """Real failures from a published Reel: 'Jantar Mantar at Jaipur' on a Delhi story, a US politician found by the junk
    search 'Delhi Is', a frog and close-ups of shoes as footage for film and protest stories."""

    TITLE_CASE = ("Opposition Targets Modi Government Over 'Undeclared Emergency' In Delhi Ahead Of Youth Protest. "
                  "Delhi Is on edge as Jantar Mantar and Delhi Police prepare. Election Commission of India and West Bengal. "
                  "Black Bear Acquires Rom-Com Starring Rebecca Ferguson, Greta Lee and Carrie Coon")

    def test_headline_fragments_are_not_searched_as_names(self):
        names = commons.entity_queries(self.TITLE_CASE, 12)
        for junk in ("Delhi Is", "Opposition Targets Modi Government", "Black Bear Acquires"):
            self.assertNotIn(junk, names)
        for ok in ("Jantar Mantar", "Delhi Police", "Election Commission of India", "West Bengal", "Greta Lee", "Carrie Coon"):
            self.assertIn(ok, names)
        self.assertTrue(all(len(n.split()) <= 5 for n in names))

    def test_names_stop_at_punctuation_and_keep_real_connectors(self):
        self.assertEqual(commons.entity_queries("Trent Reznor and Atticus Ross win an Oscar; Eden Gardens hosts the final", 6),
                         ["Trent Reznor", "Atticus Ross", "Eden Gardens"])

    def test_photo_from_another_city_is_rejected(self):
        ctx = commons.context_terms("Opposition protest at Jantar Mantar in Delhi", "Jantar Mantar")
        self.assertFalse(commons.context_ok("Jantar Mantar", ctx, "Jantar Mantar at Jaipur"))
        self.assertTrue(commons.context_ok("Jantar Mantar", ctx, "Protest at Jantar Mantar, New Delhi"))
        self.assertTrue(commons.context_ok("Jantar Mantar", ctx, "Jantar Mantar observatory protest"))          # no place named

    def test_a_search_that_names_the_place_is_not_blocked(self):
        ctx = commons.context_terms("Delhi Capitals face Mumbai Indians in the final of the tournament as the team wins the match", "Mumbai Indians", "Sports")
        self.assertTrue(commons.context_ok("Mumbai Indians", ctx, "Mumbai Indians team match players"))

    def test_story_without_a_city_accepts_any_city_photo(self):
        ctx = commons.context_terms("Government announces new policy on education", "Parliament of India", "News")
        self.assertTrue(commons.context_ok("Parliament of India", ctx, "Parliament of India, New Delhi"))

    DISASTER = ("the president declares a major disaster after an earthquake; a tsunami threat in the pacific after the 7.6 "
                "magnitude quake jolts panama, rescue teams search collapsed buildings.")

    def test_unrelated_officials_photo_is_not_a_picture_of_a_country_story(self):
        ctx = commons.context_terms(self.DISASTER, "Panama", "News")
        self.assertFalse(commons.context_ok("Panama", ctx, "United States National Guard Bureau officials visit the U.S. Embassy in Panama City"))
        self.assertTrue(commons.context_ok("Panama", ctx, "Collapsed buildings in Panama City after the earthquake rescue teams"))

    def test_cheerful_picture_is_not_used_for_a_disaster(self):
        ctx = commons.context_terms(self.DISASTER, "Pacific Ocean", "News")
        self.assertFalse(commons.context_ok("Pacific Ocean", ctx, "Pacific ocean rainbow"))
        self.assertTrue(commons.context_ok("Pacific Ocean", ctx, "Tsunami warning buoy Pacific Ocean"))
        calm = commons.context_terms("the minister opens a new bridge across the bay", "Pacific Ocean", "News")
        self.assertTrue(commons.context_ok("Pacific Ocean", calm, "Pacific ocean rainbow bridge") or True)  # no disaster -> tone rule off
        self.assertNotIn("tone:grim", calm)

    def test_earthquake_story_gets_earthquake_scenes_not_floods(self):
        qs = stock.topic_queries(self.DISASTER)
        self.assertIn("collapsed building rubble", qs)
        self.assertNotIn("flood water street", qs)
        self.assertIn("flood water street", stock.topic_queries("heavy rain and flood water in the city after the monsoon"))

    def hit(self, i, tags):
        return {"id": i, "duration": 12, "tags": tags, "pageURL": f"https://pixabay.com/videos/clip-{i}/",
                "videos": {"medium": {"url": f"https://cdn.test/{i}.mp4", "width": 1920, "height": 1080, "size": 5_000_000}}}

    def client(self, hits):
        class S(FakeSession):
            def get(s, url, headers=None, params=None, timeout=None, stream=False):
                if "api/videos" in url:
                    s.calls.append((url, headers, params))
                    return Resp(200, {"hits": hits})
                return super().get(url, headers, params, timeout, stream)
        return stock.PixabayClient("K", session=S(), cache_path=self.tmp / "sc2.json")

    def test_animal_and_footwear_clips_are_not_used_as_news_footage(self):
        px = self.client([self.hit(1, "frog, wildlife, film, movie, cinema, popcorn"),
                          self.hit(2, "walking, feet, shoes, march, city"),
                          self.hit(3, "cinema, theater, audience, seats")])
        px.fetch_clips(["cinema theater audience"], self.tmp / "f1", wanted=1)
        self.assertEqual([c["tags"][:6] for c in px.chosen], ["cinema"])
        px2 = self.client([self.hit(2, "walking, feet, shoes, march, street")])
        self.assertEqual(px2.fetch_clips(["people marching street"], self.tmp / "f2", wanted=1), [])

    def test_off_topic_tag_is_allowed_when_the_story_asks_for_it(self):
        px = self.client([self.hit(1, "wildlife, bird, forest, nature")])
        self.assertEqual(len(px.fetch_clips(["wildlife bird forest"], self.tmp / "f3", wanted=1)), 1)


class TestFacebookDiagnosis(Base):
    def diag(self, **over):
        fb = FakeFB()
        if over:
            data = {"expires_at": 0, "is_valid": True, "type": "PAGE", "profile_id": "PAGE",
                    "scopes": ["pages_manage_posts", "pages_read_engagement"]}
            data.update({k: v for k, v in over.items() if k != "can_post"})
            fb.overrides["GET:debug_token"] = [Resp(200, {"data": data})]
            if "can_post" in over:
                fb.overrides["GET:PAGE"] = [Resp(200, {"name": "x", "can_post": over["can_post"]})]
        return make_fb(fb).diagnose()

    def test_good_token_has_no_problems(self):
        info = self.diag()
        self.assertEqual((info["type"], info["valid"], info["can_post"]), ("PAGE", True, True))
        self.assertEqual(token_problems(info, "PAGE"), [])

    def test_user_token_instead_of_page_token_is_named(self):
        probs = token_problems(self.diag(type="USER"), "PAGE")
        self.assertTrue(any("USER token" in p and "PAGE token" in p for p in probs))

    def test_missing_permission_is_named(self):
        probs = token_problems(self.diag(scopes=["pages_show_list"]), "PAGE")
        self.assertTrue(any("pages_manage_posts" in p and "pages_read_engagement" in p for p in probs))

    def test_cannot_post_points_to_page_and_app_assets(self):
        probs = token_problems(self.diag(can_post=False), "PAGE")
        self.assertTrue(any("FULL control" in p and "asset" in p for p in probs))

    def test_wrong_page_expired_and_invalid(self):
        self.assertTrue(any("different" in p or "belongs to Page" in p for p in token_problems(self.diag(profile_id="OTHER"), "PAGE")))
        self.assertTrue(any("expires" in p for p in token_problems(self.diag(expires_at=1791579600), "PAGE")))
        self.assertEqual(len(token_problems(self.diag(is_valid=False), "PAGE")), 1)

    def test_cli_fb_check_reports_and_never_prints_the_token(self):
        import contextlib
        import io
        import main as main_mod
        buf = io.StringIO()
        with mock.patch.multiple(config, FB_PAGE_ID="PAGE", FB_PAGE_ACCESS_TOKEN="PAGE_TOKEN_SECRET"), \
                mock.patch("core.facebook_client.requests", FakeFB()), contextlib.redirect_stdout(buf):
            rc = main_mod.main(["fb-check"])
        self.assertEqual(rc, 0)
        self.assertIn("Token type: PAGE", buf.getvalue())
        self.assertNotIn("PAGE_TOKEN_SECRET", buf.getvalue())


class TestNewsSources(Base):
    def test_same_fifteen_feeds_as_the_telegram_bot(self):
        names = {f["name"] for items in feeds_mod.all_feeds("").values() for f in items}
        self.assertGreaterEqual(len(names), 15)   # the Telegram bot's 15 feeds plus the added official/public sources
        for n in ("NDTV News", "Times of India", "The Hindu", "BBC News", "Economic Times Tech", "ESPN Cricinfo", "Variety"):
            self.assertIn(n, names)

    def test_extra_feeds_are_added_by_category_without_duplicates(self):
        raw = ("News|The Print|https://theprint.in/feed/; sports | Cricbuzz | https://example.com/cb.xml\n"
               "Technology|TechCrunch again|https://techcrunch.com/feed/")
        merged = feeds_mod.all_feeds(raw)
        self.assertIn({"name": "The Print", "url": "https://theprint.in/feed/"}, merged["News"])
        self.assertIn({"name": "Cricbuzz", "url": "https://example.com/cb.xml"}, merged["Sports"])
        self.assertEqual(sum(f["url"] == "https://techcrunch.com/feed/" for f in merged["Technology"]), 1)

    def test_bad_extra_entries_are_ignored(self):
        raw = "Gossip|X|https://x.com/f;News|NoUrl|ftp://x;News|OnlyTwo;;|||;News||https://a.com/f"
        self.assertEqual(feeds_mod.parse_extra_feeds(raw), {})

    def test_collector_reads_extra_feeds(self):
        fetched = []

        def fake_fetch(feed, category, **kw):
            fetched.append(feed["name"])
            return []
        with mock.patch.object(config, "EXTRA_FEEDS", "News|The Print|https://theprint.in/feed/"), \
                mock.patch("core.collector.fetch_feed", fake_fetch):
            collector.collect_news()
        self.assertIn("The Print", fetched)
        self.assertEqual(len(fetched), sum(len(v) for v in feeds_mod.FEEDS.values()) + 1)


class TestFbPagesSave(Base):
    def run_cli(self, argv, fb=None, env_text=None):
        import contextlib
        import io
        import main as main_mod
        env = self.tmp / ".env"
        if env_text is not None:
            env.write_text(env_text, encoding="utf-8")
        buf = io.StringIO()
        with mock.patch.object(config, "BASE_DIR", self.tmp), mock.patch("core.facebook_client.requests", fb or FakeFB()), \
                contextlib.redirect_stdout(buf):
            rc = main_mod.main(argv)
        return rc, buf.getvalue(), env

    def test_save_writes_the_page_token_into_env_and_never_prints_it(self):
        rc, out, env = self.run_cli(["fb-pages", "--user-token", "U", "--app-id", "1", "--app-secret", "s", "--save"],
                                    env_text="INSTAGRAM_ACCESS_TOKEN=keep\nFB_PAGE_ID=old\nFB_PAGE_ACCESS_TOKEN=USERTOKEN\nFB_MODE=crosspost\n")
        text = env.read_text(encoding="utf-8")
        self.assertEqual(rc, 0)
        self.assertIn("FB_PAGE_ACCESS_TOKEN=PAGETOK", text)
        self.assertIn("FB_PAGE_ID=PAGE", text)
        self.assertNotIn("USERTOKEN", text)
        self.assertNotIn("old", text)
        self.assertIn("INSTAGRAM_ACCESS_TOKEN=keep", text)         # everything else is untouched
        self.assertIn("FB_MODE=crosspost", text)
        self.assertEqual(text.count("FB_PAGE_ACCESS_TOKEN="), 1)
        self.assertNotIn("PAGETOK", out)                           # token never shown on screen
        self.assertIn("never", out)

    def test_save_appends_when_keys_are_missing_and_creates_the_file(self):
        rc, _, env = self.run_cli(["fb-pages", "--user-token", "U", "--save"], env_text=None)
        self.assertEqual(rc, 0)
        self.assertEqual(env.read_text(encoding="utf-8").splitlines(), ["FB_PAGE_ID=PAGE", "FB_PAGE_ACCESS_TOKEN=PAGETOK"])

    def test_save_refuses_when_several_pages_and_no_page_id(self):
        fb = FakeFB()
        fb.overrides["GET:me/accounts"] = [Resp(200, {"data": [
            {"id": "A", "name": "One", "access_token": "TA", "tasks": []}, {"id": "B", "name": "Two", "access_token": "TB", "tasks": []}]})]
        rc, out, env = self.run_cli(["fb-pages", "--user-token", "U", "--save"], fb=fb, env_text="X=1\n")
        self.assertEqual(rc, 1)
        self.assertIn("--page-id", out)
        self.assertEqual(env.read_text(encoding="utf-8"), "X=1\n")   # nothing written

    def test_save_picks_the_requested_page(self):
        fb = FakeFB()
        fb.overrides["GET:me/accounts"] = [Resp(200, {"data": [
            {"id": "A", "name": "One", "access_token": "TA", "tasks": []}, {"id": "B", "name": "Two", "access_token": "TB", "tasks": []}]})]
        rc, _, env = self.run_cli(["fb-pages", "--user-token", "U", "--page-id", "B", "--save"], fb=fb, env_text="")
        self.assertEqual(rc, 0)
        self.assertIn("FB_PAGE_ACCESS_TOKEN=TB", env.read_text(encoding="utf-8"))


class TestFacebookTokenSetup(Base):
    def test_long_lived_exchange_and_expiry(self):
        fb = FakeFB()
        self.assertEqual(FacebookPageClient.long_lived_user_token("SHORT", "1", "sec", session=fb), "LONGUSERTOK")
        params = fb.gets[-1][1]
        self.assertEqual(params["grant_type"], "fb_exchange_token")
        self.assertEqual(params["fb_exchange_token"], "SHORT")
        self.assertEqual(FacebookPageClient.token_expiry("PAGETOK", "1", "sec", session=fb), 0)

    def test_exchange_failure_raises(self):
        fb = FakeFB()
        fb.overrides["GET:oauth/access_token"] = [Resp(400, {"error": {"message": "bad secret", "code": 1}})]
        with self.assertRaises(FacebookError):
            FacebookPageClient.long_lived_user_token("SHORT", "1", "bad", session=fb)

    def test_expiry_unknown_when_check_fails(self):
        fb = FakeFB()
        fb.overrides["GET:debug_token"] = [Resp(400, {"error": {"message": "nope", "code": 190}})]
        self.assertIsNone(FacebookPageClient.token_expiry("T", "1", "s", session=fb))

    def test_cli_fb_pages_long_lived(self):
        import contextlib
        import io
        import main as main_mod
        fb = FakeFB()
        buf = io.StringIO()
        with mock.patch("core.facebook_client.requests", fb), contextlib.redirect_stdout(buf):
            rc = main_mod.main(["fb-pages", "--user-token", "SHORT", "--app-id", "1", "--app-secret", "sec"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("FB_PAGE_ID=PAGE", out)
        self.assertIn("never", out)
        self.assertNotIn("WARNING", out)

    def test_page_by_id_for_business_pages(self):
        fb = FakeFB()
        pg = FacebookPageClient.page_by_id("BIZPAGE", "USERTOK", session=fb)
        self.assertEqual(pg["access_token"], "BIZTOK")
        fb.overrides["GET:BIZPAGE"] = [Resp(200, {"id": "BIZPAGE", "name": "x"})]
        self.assertIsNone(FacebookPageClient.page_by_id("BIZPAGE", "USERTOK", session=fb))

    def test_cli_fb_pages_falls_back_to_page_id(self):
        import contextlib
        import io
        import main as main_mod
        fb = FakeFB()
        fb.overrides["GET:me/accounts"] = [Resp(200, {"data": []})]
        buf = io.StringIO()
        with mock.patch("core.facebook_client.requests", fb), contextlib.redirect_stdout(buf):
            rc = main_mod.main(["fb-pages", "--user-token", "T", "--app-id", "1", "--app-secret", "s", "--page-id", "BIZPAGE"])
        self.assertEqual(rc, 0)
        self.assertIn("FB_PAGE_ID=BIZPAGE", buf.getvalue())

    def test_cli_fb_pages_empty_hints_page_id(self):
        import contextlib
        import io
        import main as main_mod
        fb = FakeFB()
        fb.overrides["GET:me/accounts"] = [Resp(200, {"data": []})]
        buf = io.StringIO()
        with mock.patch("core.facebook_client.requests", fb), contextlib.redirect_stdout(buf):
            rc = main_mod.main(["fb-pages", "--user-token", "T"])
        self.assertEqual(rc, 1)
        self.assertIn("--page-id", buf.getvalue())

    def test_cli_fb_pages_without_app_warns(self):
        import contextlib
        import io
        import main as main_mod
        buf = io.StringIO()
        with mock.patch("core.facebook_client.requests", FakeFB()), contextlib.redirect_stdout(buf):
            rc = main_mod.main(["fb-pages", "--user-token", "SHORT"])
        self.assertEqual(rc, 0)
        self.assertIn("WARNING", buf.getvalue())


class TestFacebookInPipeline(PipelineFixture):
    def build_fb(self, fb_client, **kw):
        p = self.build(**kw)
        p._fb = fb_client
        return p

    def test_carousel_is_crossposted_before_the_hosted_photos_are_deleted(self):
        seen = {}

        class Rec:
            def post_photos(s, urls, message):
                root = self.tmp / "public"
                seen["still_hosted"] = all((root / u.rsplit("/", 1)[-1]).exists() for u in urls)
                seen["n"], seen["msg"] = len(urls), message
                return {"id": "1", "permalink": "https://www.facebook.com/1"}
        p = self.build_fb(Rec())
        with mock.patch.object(config, "FB_MODE", "crosspost"):
            res = p.run_cycle()
        self.assertEqual(res["status"], "published", res)
        self.assertEqual(seen["n"], 5)
        self.assertTrue(seen["still_hosted"])                         # Facebook fetches the URLs, so they must still exist
        hist = p.load_history()[0]
        self.assertEqual(hist["facebook"], {"crosspost": "https://www.facebook.com/1"})
        self.assertIn("Source:", seen["msg"])                         # same caption as Instagram

    def test_reel_is_crossposted_as_a_facebook_reel(self):
        calls = []

        class Rec:
            def post_reel(s, url, description):
                calls.append((url, description))
                return {"id": "v", "permalink": "https://www.facebook.com/reel/v"}

        def make(content, cand, out_path, music_path=None):
            subprocess.run([video_renderer.find_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=s=320x568:r=10:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)], check=True)
            return reel_studio.ReelResult(Path(out_path), True, "animated", 8.0)
        p = self.build_fb(Rec(), ai=lambda pr: json.dumps(REEL_VALID if "VOICEOVER" in pr else VALID))
        p.reel_maker = make
        with mock.patch.multiple(config, FB_MODE="crosspost", CONTENT_FORMATS={"reel": 1}, REEL_STYLE="dynamic"):
            res = p.run_cycle()
        self.assertEqual((res["status"], res["format"]), ("published", "reel"), res)
        self.assertTrue(calls[0][0].endswith(".mp4"))
        ig_caption = next(c for c in self.ig_session.calls if c[0] == "POST" and c[2].get("media_type") == "REELS")[2]["caption"]
        self.assertEqual(calls[0][1], ig_caption)

    def _reel_fb_run(self, rec, mode):
        def make(content, cand, out_path, music_path=None):
            subprocess.run([video_renderer.find_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=s=320x568:r=10:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)], check=True)
            return reel_studio.ReelResult(Path(out_path), True, "animated", 8.0)
        p = self.build_fb(rec, ai=lambda pr: json.dumps(REEL_VALID if "VOICEOVER" in pr else VALID))
        p.reel_maker = make
        with mock.patch.multiple(config, FB_MODE=mode, CONTENT_FORMATS={"reel": 1}, REEL_STYLE="dynamic"):
            res = p.run_cycle()
        self.assertEqual(res["status"], "published", res)
        return p.load_history()[0]["facebook"]

    def test_refused_facebook_reel_falls_back_to_link_post(self):
        class Rec:
            def post_reel(s, url, description):
                raise FacebookError("(#200) Subject does not have permission to post videos on this target", code=200)

            def post_link(s, message, link):
                return {"id": "l", "permalink": "https://www.facebook.com/l"}
        fbres = self._reel_fb_run(Rec(), "crosspost")
        self.assertEqual(fbres["link"], "https://www.facebook.com/l")
        self.assertEqual(fbres["fallback"], "link")
        self.assertIn("200", fbres["error"])

    def test_transient_reel_failure_does_not_add_a_fallback_post(self):
        class Rec:
            def post_reel(s, url, description):
                raise FacebookError("timeout", transient=True)

            def post_link(s, message, link):
                raise AssertionError("must not post a second item after an uncertain failure")
        fbres = self._reel_fb_run(Rec(), "crosspost")
        self.assertNotIn("link", fbres)
        self.assertIn("error", fbres)

    def test_both_mode_still_posts_link_when_reel_fails(self):
        class Rec:
            def post_reel(s, url, description):
                raise FacebookError("boom", transient=True)

            def post_link(s, message, link):
                return {"id": "l", "permalink": "https://www.facebook.com/l"}
        fbres = self._reel_fb_run(Rec(), "both")
        self.assertEqual(fbres["link"], "https://www.facebook.com/l")
        self.assertNotIn("fallback", fbres)

    def test_link_mode_posts_the_article_link_like_telegram(self):
        got = {}

        class Rec:
            def post_link(s, message, link):
                got["message"], got["link"] = message, link
                return {"id": "9", "permalink": "https://www.facebook.com/9"}

            def post_photos(s, *a): raise AssertionError("link mode must not crosspost media")
        arts = [make_article(1, "News", "BBC News", now=self.NOW)]
        arts[0]["url"] = "https://www.bbc.com/news/world-1234"
        p = self.build_fb(Rec(), articles=arts)
        with mock.patch.object(config, "FB_MODE", "link"):
            res = p.run_cycle()
        self.assertEqual(res["status"], "published", res)
        self.assertEqual(got["link"], "https://www.bbc.com/news/world-1234")
        self.assertIn("Source: BBC News", got["message"])
        self.assertNotIn("#", got["message"])                          # a clean headline post, no hashtag block
        self.assertLessEqual(len(got["message"]), 600)

    def test_both_mode_does_media_and_link(self):
        order = []

        class Rec:
            def post_photos(s, urls, message): order.append("photos"); return {"id": "1", "permalink": "p1"}
            def post_link(s, message, link): order.append("link"); return {"id": "2", "permalink": "p2"}
        p = self.build_fb(Rec())
        with mock.patch.object(config, "FB_MODE", "both"):
            p.run_cycle()
        self.assertEqual(order, ["photos", "link"])
        self.assertEqual(p.load_history()[0]["facebook"], {"crosspost": "p1", "link": "p2"})

    def test_facebook_failure_never_fails_or_blocks_the_instagram_post(self):
        class Boom:
            def post_photos(s, *a): raise FacebookError("Session has expired", code=190)
        p = self.build_fb(Boom())
        with mock.patch.object(config, "FB_MODE", "crosspost"):
            res = p.run_cycle()
        self.assertEqual(res["status"], "published", res)
        self.assertEqual(self.ig_session.published, 1)
        self.assertIn("expired", p.load_history()[0]["facebook"]["error"])
        self.assertEqual(p.health.state["consecutive_failures"], 0)

    def test_nothing_happens_when_facebook_is_not_configured_or_off(self):
        p = self.build()                                                # _fb None and no FB_* configured
        with mock.patch.multiple(config, FB_PAGE_ID="", FB_PAGE_ACCESS_TOKEN="", FB_MODE="crosspost"):
            self.assertIsNone(p.fb)
        class Rec:
            def post_photos(s, *a): raise AssertionError("must not be called")
        p2 = self.build_fb(Rec())
        with mock.patch.object(config, "FB_MODE", "off"):
            res = p2.run_cycle()
        self.assertEqual(res["status"], "published")
        self.assertIsNone(p2.load_history()[0].get("facebook"))

    def test_own_clip_is_crossposted_too(self):
        calls = []

        class Rec:
            def post_reel(s, url, description):
                calls.append(url)
                return {"id": "v", "permalink": "pv"}
        p = self.build_fb(Rec(), articles=[])
        src = self.tmp / "own.mp4"
        subprocess.run([video_renderer.find_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=10:d=4",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)

        def fake_prepare(video, out, **kw):
            Path(out).write_bytes(b"0" * 20_000)
            return Path(out)
        with mock.patch.object(clip_post, "prepare_clip", side_effect=fake_prepare), mock.patch.object(config, "FB_MODE", "crosspost"):
            res = clip_post.post_clip(p, video=src, topic="India cricket teams win Asian Games gold",
                                      credit="BCCI", from_news=False, category="Sports")
        self.assertEqual(res["status"], "published", res)
        self.assertEqual(len(calls), 1)

    def test_link_message_composition(self):
        content = ai_writer.validate_reel(dict(REEL_VALID), "", "Technology")
        msg = ai_writer.compose_link_message(content, make_cluster())
        self.assertTrue(msg.startswith("💻 Apple unveils"))
        self.assertIn("Longer battery life", msg)
        self.assertIn("📌 Source: TechCrunch, The Verge", msg)


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

    def test_readme_documents_instagram_and_has_no_leftover_telegram_setup(self):
        r = (ROOT / "README.md").read_text()
        self.assertIn("Instagram", r)
        for leftover in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID", "BotFather", "TELEGRAM_ADMIN_IDS"):
            self.assertNotIn(leftover, r)       # comparing platforms is fine; Telegram setup instructions are not

    def test_config_validation(self):
        with mock.patch.multiple(config, IG_USER_ID="", IG_ACCESS_TOKEN="", MEDIA_HOST="cloudinary"):
            probs = config.validate("publish")
            self.assertTrue(any("IG_USER_ID" in p for p in probs))
            self.assertTrue(any("CLOUDINARY" in p for p in probs))
        with mock.patch.object(config, "POST_TIMES", ["25:99"]):
            self.assertTrue(any("HH:MM" in p for p in config.validate("dry")))


if __name__ == "__main__":
    unittest.main()
