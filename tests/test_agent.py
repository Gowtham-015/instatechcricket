"""Offline test-suite (no network, no real credentials). Run:  python -m unittest discover -s tests -v"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import requests
from PIL import Image

from core import ai_writer, config, dedupe, health, image_renderer, media_host, ranker, schedule, storage
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


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        p = mock.patch.multiple(config, DATA_DIR=self.tmp, PREVIEW_DIR=self.tmp / "preview",
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
class TestPipeline(Base):
    NOW = datetime(2026, 10, 5, 3, 40, tzinfo=timezone.utc)  # 09:10 IST -> slot 09:00 due

    def build(self, articles=None, ig=None, now_fn=None, ai=lambda p: json.dumps(VALID)):
        arts = articles if articles is not None else [make_article(i, c, s, now=self.NOW) for i, (c, s) in enumerate(
            [("News", "BBC News"), ("Technology", "Wired"), ("Sports", "BBC Sport"), ("Entertainment", "Variety")])]
        self.ig_session = ig or FakeIG()
        self.host = media_host.LocalHost("https://cdn.test/media", self.tmp / "public")
        return Pipeline(collector=lambda: arts, writer=AIWriter(generate_json=ai, cache_path=self.tmp / "ai.json", sleep=lambda s: None),
                        host=self.host, ig=make_ig(self.ig_session), now_fn=now_fn or (lambda: self.NOW),
                        data_dir=self.tmp, sleep=lambda s: None, verify_public=lambda u: True)

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
