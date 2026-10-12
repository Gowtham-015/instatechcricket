"""Editorial newsroom tests: story-aware format choice, authentic-media retrieval, provenance, safe publishing.

Offline by design: every external service (feeds, photo/video providers, HTTP downloads, Instagram, media host, Gemini,
Google Trends) is replaced by a fake. Nothing here can publish to a real account or use an API quota.

Run:  python -m pytest -q tests/test_editorial.py        (or: python -m unittest discover -s tests)
Numbering N01..N18 follows the 18 scenarios of the upgrade brief; the remaining tests cover defects found in the audit.
"""
from __future__ import annotations

import json
import math
import random
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests
import yaml
from PIL import Image

try:                                    # `pytest` (package import) and `unittest discover -s tests` (flat import)
    from .test_agent import FakeIG, PipelineFixture, REEL_VALID, Resp, VALID, make_ig
except ImportError:                     # pragma: no cover
    from test_agent import FakeIG, PipelineFixture, REEL_VALID, Resp, VALID, make_ig

from core import (ai_writer, collector, config, editorial, media_host, media_match, photo_post, provenance, ranker, story,
                  storage, trends as trends_mod, video_post, video_renderer)
from core.ai_writer import AIWriter, ContentError, compose_caption, validate_content
from core.dedupe import filter_published, similar_to_history
from core.editorial import decide
from core.instagram_client import InstagramError
from core.media_match import MediaRetriever, assess, different_match, strip_roles
from core.provenance import MediaAsset, MediaPlan
from core.stock import overlap, query_terms
from core.story import StoryProfile

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 5, 3, 40, tzinfo=timezone.utc)          # 09:10 IST: the 09:00 slot is due


# ------------------------------------------------------------------ stories
STORIES = {
    "final": dict(
        category="Sports",
        title="India beat England to win the Women's World Cup final in Navi Mumbai",
        summary="India defeated England by 52 runs in the final on Sunday night. Captain Harmanpreet Kaur lifted the "
                "trophy in front of a packed stadium. It is India's first title in the tournament."),
    "policy": dict(
        category="News",
        title="Finance Minister Nirmala Sitharaman announces new GST relief for small traders",
        summary="Finance Minister Nirmala Sitharaman said on Monday that small traders will get relief under the revised "
                "GST framework. The change takes effect next month."),
    "phone": dict(
        category="Technology",
        title="Samsung launches Galaxy S27 Ultra in India with 200MP camera",
        summary="Samsung launched the Galaxy S27 Ultra in India on Tuesday. The phone has a 200MP camera and a 6.9-inch "
                "display. Prices start at Rs 1,29,999."),
    "rbi": dict(
        category="Business",
        title="RBI keeps repo rate unchanged at 6.5 per cent",
        summary="The Reserve Bank of India kept the repo rate unchanged at 6.5 per cent on Friday. Governor Sanjay "
                "Malhotra announced the decision after the policy meeting."),
    "explainer": dict(
        category="Technology",
        title="Explained: why the new data protection rules matter for everyday app users",
        summary="The government notified the new data protection rules this week, and companies now have to explain what "
                "personal data their apps collect and why they need it. Users can ask for their data to be deleted, and "
                "firms that ignore the rules face penalties set out in the framework. Industry groups say the changes will "
                "take months to roll out across large and small apps, and regulators will publish the timeline later."),
    "multi": dict(
        category="News",
        title="Parliament committee reviews telecom licensing framework after consultation",
        summary="A parliamentary committee reviewed the telecom licensing framework on Tuesday after a public consultation "
                "that drew many responses. Members asked the ministry to explain how spectrum fees would be calculated. "
                "The committee also sought details on how rural coverage targets would be monitored every year. Officials "
                "promised to return with a revised draft before the next session of the house."),
}


def articles_for(key: str, sources=("BBC News", "The Hindu"), age_min=30, now=NOW, first_id=1, media=None) -> list[dict]:
    s = STORIES[key]
    out = []
    for i, src in enumerate(sources):
        out.append({"id": f"{key}{first_id + i}", "title": s["title"], "summary": s["summary"], "source": src,
                    "url": f"https://news.example/{key}/{first_id + i}", "category": s["category"],
                    "published": (now - timedelta(minutes=age_min + i)).isoformat(), "media": (media if i == 0 else None) or []})
    return out


def cluster_for(key: str, **kw) -> dict:
    return ranker.build_clusters(articles_for(key, **kw), NOW)[0]


def profile_for(cl: dict) -> StoryProfile:
    return story.analyse(cl, NOW)


# ------------------------------------------------------------------ media fakes
def make_jpg(path: Path, seed: int, size=(1400, 900)) -> Path:
    rnd = random.Random(seed)
    small = Image.new("RGB", (8, 8))
    small.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(64)])
    img = Image.blend(small.resize(size, Image.BICUBIC), Image.effect_noise(size, 30).convert("RGB"), 0.15)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=90)
    return Path(path)


_MP4_CACHE: dict[str, Path] = {}


def make_mp4(directory: Path, name="clip.mp4", seconds=4, audio=True, size="640x480") -> Path:
    out = Path(directory) / name
    cmd = [video_renderer.find_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=25:d={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"] + (["-c:a", "aac", "-shortest"] if audio else []) + [str(out)]
    subprocess.run(cmd, check=True)
    return out


def photo_item(title, *, text="", license="CC BY 4.0", creator="Jane Doe", date="2026-10-04", seed=1, width=1400,
               height=900, url=None, bad=None, page_url=None):
    return {"title": title, "text": text, "license": license, "creator": creator, "date": date, "seed": seed,
            "width": width, "height": height, "url": url or f"https://commons.example/{seed}-{abs(hash(title)) % 9999}.jpg",
            "credit": f"{creator} ({license})", "needs_credit": not license.lower().startswith(("cc0", "public")),
            "via": "Wikimedia Commons", "page_url": page_url or f"https://commons.example/wiki/{seed}", "bad": bad}


def _scored(items, query):
    terms = query_terms(query)
    out = []
    for it in items:
        sc = overlap(terms, it["title"] + " " + it.get("text", ""))
        if sc >= max(1, math.ceil(0.6 * len(terms))):
            out.append({**it, "score": sc})
    return out


class FakePhotos:
    name, enabled = "fake", True

    def __init__(self, items):
        self.items, self.downloads = list(items), []

    def candidates(self, query, context=None):
        return _scored(self.items, query)

    def download(self, url, dest):
        self.downloads.append(url)
        it = next(i for i in self.items if i["url"] == url)
        if it.get("bad") == "html":
            Path(dest).write_text("<html>" + "error page " * 800 + "</html>")
        elif it.get("bad") == "http":
            raise RuntimeError("download HTTP 503")
        else:
            make_jpg(dest, it["seed"], (it["width"], it["height"]))
        return dest


class FakeVideos:
    enabled = True

    def __init__(self, items, mp4: Path):
        self.items, self.mp4, self.downloads = list(items), mp4, []

    def video_candidates(self, query, context=None):
        return _scored(self.items, query)

    def download(self, url, dest):
        self.downloads.append(url)
        shutil.copyfile(self.mp4, dest)
        return dest


class FakeHTTP:
    """requests-like session for `official` media attached to feed items (robots.txt + the file)."""

    def __init__(self, mp4: Path | None = None, robots="User-agent: *\nAllow: /\n"):
        self.calls, self.mp4, self.robots = [], mp4, robots

    def get(self, url, headers=None, timeout=None, stream=False, **kw):
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            return mock.Mock(status_code=200, text=self.robots)
        data = self.mp4.read_bytes() if self.mp4 else b"x" * 100
        return mock.Mock(status_code=200, iter_content=lambda n: iter([data]))


def video_item(title, *, text="", license="CC BY 4.0", date="2026-10-04", depicts="event", creator="Match Org"):
    return {"title": title, "text": text, "license": license, "creator": creator, "date": date, "depicts": depicts,
            "url": f"https://commons.example/{abs(hash(title)) % 9999}.mp4", "credit": f"{creator} ({license})",
            "needs_credit": True, "via": "Wikimedia Commons", "page_url": "https://commons.example/wiki/v", "keep_audio": True}


class Env(unittest.TestCase):
    """Editorial policy + clean temp dirs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.multiple(config, DATA_DIR=self.tmp, PREVIEW_DIR=self.tmp / "preview", FORMAT_POLICY="editorial",
                                MEDIA_SOURCE_ORDER=["official", "commons"], MEDIA_MIN_WIDTH=1000, MEDIA_MIN_HEIGHT=600,
                                MEDIA_MIN_CONFIDENCE=0.6, ALLOW_FILE_PHOTOS=True, PHOTO_DIR="", VIDEO_DIR="",
                                TRENDS_ENABLED=False, MIN_SOURCES=1, ACCOUNT_HANDLE="@dailypulse", BRAND_NAME="Daily Pulse",
                                ALERT_WEBHOOK_URL="", TTS_PROVIDER="none", REEL_STYLE="dynamic", REEL_VARIETY=False,
                                MUSIC_PATH="", FB_PAGE_ID="", FB_PAGE_ACCESS_TOKEN="", PEXELS_API_KEY="", PIXABAY_API_KEY="")
        p.start()
        self.addCleanup(p.stop)

    def retrieve(self, key="final", *, photos=None, videos=None, mp4=None, session=None, media=None, stock=False,
                 **kw) -> tuple[MediaPlan, StoryProfile, dict]:
        cl = cluster_for(key, media=media, **kw)
        prof = profile_for(cl)
        pho = {"commons": FakePhotos(photos)} if photos is not None else {}
        vid = {"commons": FakeVideos(videos, mp4)} if videos is not None else {}
        r = MediaRetriever(photo_sources=pho, video_sources=vid, official_domains=["pib.gov.in"], session=session,
                           stock_available=stock)
        work = self.tmp / "work"
        work.mkdir(exist_ok=True)
        plan = r.retrieve(cl, prof, work, None, NOW)
        plan.sources = {"photos": pho.get("commons"), "videos": vid.get("commons")}
        return plan, prof, cl


def asset(kind="photo", *, role="event", conf=0.9, permission="open_licence", title="Photo", path="", duration=0.0,
          width=1400, height=900, audio=False, license="CC BY 4.0", creator="Jane Doe") -> MediaAsset:
    return MediaAsset(kind=kind, source="commons", title=title, original_url="https://commons.example/a", page_url="https://commons.example/p",
                      creator=creator, license=license, permission=permission, attribution=f"{creator} ({license})",
                      attribution_required=True, role=role, confidence=conf, story_id="s", path=path, width=width,
                      height=height, duration=duration, has_audio=audio, retrieved_at="2026-10-05T03:40:00+00:00")


def profile(**kw) -> StoryProfile:
    base = dict(story_id="s", title="t", text="t", category="News", confidence=0.8, independent_sources=3,
                narration_value=0.2, simple_update=True, fact_points=2, event_story=False)
    base.update(kw)
    return StoryProfile(**base)


# =====================================================================================================================
# N01-N04: the format decision engine (pure rules)
# =====================================================================================================================
class TestFormatDecision(Env):
    def test_n01_authorised_original_video_is_evaluated_and_chosen(self):
        plan = MediaPlan("s", videos=[asset("video", duration=12, audio=True, permission="official_permitted")],
                         photos=[asset(title="Photo")])
        d = decide(profile(event_story=True, category="Sports"), plan)
        self.assertEqual(d.format, "video")
        self.assertEqual(d.media_use, "event_media")
        self.assertIn("original audio is preserved", d.reason)
        self.assertTrue(any(t.startswith("V: authorised event video") for t in d.trail))

    def test_n01_a_video_that_is_not_event_footage_or_too_short_is_not_chosen(self):
        for v in (asset("video", role="subject", duration=12), asset("video", duration=1.0),
                  asset("video", duration=12, permission="unverified")):
            d = decide(profile(), MediaPlan("s", videos=[v], photos=[asset()]))
            self.assertNotEqual(d.format, "video", v)

    def test_n02_simple_update_with_a_relevant_photo_is_a_photo_post_not_a_reel(self):
        plan = MediaPlan("s", photos=[asset()], illustrative_clips=3)
        d = decide(profile(simple_update=True, narration_value=0.1, category="Sports", event_story=True), plan)
        self.assertEqual(d.format, "photo")
        self.assertIn("verified event photograph available", d.reason)
        self.assertIn("original video unavailable", d.reason)
        self.assertFalse(d.voiceover)

    def test_n02_a_file_photo_is_labelled_as_such(self):
        d = decide(profile(), MediaPlan("s", photos=[asset(role="subject")]))
        self.assertEqual((d.format, d.media_use), ("photo", "file_photo"))
        self.assertIn("file photo", d.reason)

    def test_n03_several_relevant_images_plus_distinct_facts_make_a_carousel(self):
        plan = MediaPlan("s", photos=[asset(title=f"P{i}") for i in range(3)])
        d = decide(profile(simple_update=False, fact_points=4, narration_value=0.3), plan)
        self.assertEqual(d.format, "carousel")
        self.assertEqual(len(d.photos), 3)

    def test_n03_many_photos_alone_do_not_force_a_carousel_for_a_simple_update(self):
        plan = MediaPlan("s", photos=[asset(title=f"P{i}") for i in range(3)])
        d = decide(profile(simple_update=True, fact_points=4), plan)
        self.assertEqual(d.format, "photo")

    def test_n04_an_explanatory_story_that_benefits_from_narration_is_a_reel(self):
        plan = MediaPlan("s", photos=[asset()])
        d = decide(profile(simple_update=False, narration_value=0.8, fact_points=2), plan)
        self.assertEqual(d.format, "reel")
        self.assertTrue(d.voiceover)
        self.assertIn("narration adds useful context", d.reason)

    def test_n04_narration_never_forces_a_reel_without_authentic_visuals_for_an_event_story(self):
        plan = MediaPlan("s", photos=[], illustrative_clips=5)
        d = decide(profile(simple_update=False, narration_value=0.9, category="Sports", event_story=True), plan)
        self.assertEqual(d.format, "card")

    def test_reel_voiceover_preference(self):
        plan = MediaPlan("s", photos=[asset()])
        pr = profile(simple_update=False, narration_value=0.8)
        with mock.patch.object(config, "REEL_VOICEOVER", "never"):
            self.assertFalse(decide(pr, plan).voiceover)
        pr2 = profile(simple_update=False, narration_value=0.65)
        with mock.patch.object(config, "REEL_VOICEOVER", "always"):
            self.assertTrue(decide(pr2, plan).voiceover)
        self.assertFalse(decide(pr, plan, tts_ok=False).voiceover)

    def test_content_formats_weights_cannot_override_the_editorial_decision(self):
        with mock.patch.object(config, "CONTENT_FORMATS", {"reel": 100}):
            d = decide(profile(), MediaPlan("s", photos=[asset()]))
        self.assertEqual(d.format, "photo")

    def test_encoder_failure_falls_back_to_a_photo(self):
        plan = MediaPlan("s", photos=[asset()])
        d = decide(profile(simple_update=False, narration_value=0.9), plan, encoder_ok=False)
        self.assertEqual(d.format, "photo")

    def test_illustrative_footage_is_allowed_only_for_non_event_categories_and_is_labelled(self):
        plan = MediaPlan("s", photos=[], illustrative_clips=4)
        d = decide(profile(simple_update=False, narration_value=0.9, category="Technology", event_story=False), plan)
        self.assertEqual((d.format, d.media_use, d.illustrative_footage), ("reel", "licensed_illustrative", True))

    def test_operator_override_is_respected_when_feasible_and_recorded(self):
        d = decide(profile(), MediaPlan("s", photos=[asset()]), forced="card")
        self.assertEqual((d.format, d.forced), ("card", True))
        d = decide(profile(), MediaPlan("s", photos=[]), forced="photo")        # impossible -> decided automatically
        self.assertEqual(d.format, "card")
        self.assertTrue(any("not possible" in t for t in d.trail))

    def test_held_story_is_never_published(self):
        d = decide(profile(hold_reason="held: thin sourcing"), MediaPlan("s", photos=[asset()]))
        self.assertEqual(d.format, "hold")


# =====================================================================================================================
# N05-N10: media retrieval, rejection, provenance, fallbacks
# =====================================================================================================================
class TestAuthenticMedia(Env):
    def test_n05_generic_stock_footage_is_never_accepted_as_event_media(self):
        prof = profile_for(cluster_for("final"))
        a = assess({"title": "Cricket stadium crowd cheering stock footage", "text": "India England final", "generic_stock": True,
                    "permission": "open_licence"}, prof, kind="video")
        self.assertFalse(a.accepted)
        self.assertIn("generic stock", a.reason)

    def test_n05_stock_only_means_no_event_media_and_no_reel_for_an_event_story(self):
        plan, prof, _ = self.retrieve("final", photos=[], stock=True)
        self.assertEqual(plan.photos, [])
        self.assertEqual(plan.illustrative_clips, 1)              # available as labelled illustration only
        d = decide(prof, plan)
        self.assertNotEqual(d.format, "reel")
        self.assertEqual(d.media_use, "editorial_graphic")

    def test_n06_photo_of_the_wrong_person_is_rejected(self):
        items = [photo_item("Smriti Irani addresses a rally", text="Union minister Smriti Irani; Nirmala Sitharaman is mentioned", seed=1),
                 photo_item("Nirmala Sitharaman at the GST Council meeting", text="GST Council", date="2024-07-22", seed=2)]
        plan, prof, _ = self.retrieve("policy", photos=items)
        rejected = {r.title: r.reason for r in plan.rejected}
        self.assertIn("wrong person", rejected["Smriti Irani addresses a rally"])
        self.assertEqual([a.title for a in plan.photos], ["Nirmala Sitharaman at the GST Council meeting"])
        self.assertEqual(plan.photos[0].role, "subject")          # right person, old photo: a labelled file photo only

    def test_n06_a_photo_that_never_names_the_subject_is_rejected_for_insufficient_evidence(self):
        plan, _, _ = self.retrieve("policy", photos=[photo_item("Parliament building at dusk", text="Delhi", seed=3)])
        self.assertEqual(plan.photos, [])

    def test_n06_a_role_prefix_in_the_headline_does_not_block_the_right_person(self):
        self.assertEqual(strip_roles("Finance Minister Nirmala Sitharaman"), "Nirmala Sitharaman")
        self.assertEqual(strip_roles("Captain Harmanpreet Kaur"), "Harmanpreet Kaur")
        self.assertEqual(strip_roles("Law Commission"), "Law Commission")              # an institution, not a person
        self.assertTrue(media_match._entity_matched("Captain Harmanpreet Kaur", "Harmanpreet Kaur lifts the trophy"))

    def test_n06_a_product_model_number_must_match(self):
        self.assertFalse(media_match._entity_matched("Galaxy S27 Ultra", "Samsung Galaxy S26 Ultra review"))
        self.assertTrue(media_match._entity_matched("Galaxy S27 Ultra", "Samsung Galaxy S27 Ultra launch event"))
        items = [photo_item("Samsung Galaxy S26 Ultra hands-on", text="Samsung Galaxy S26 Ultra", seed=4),
                 photo_item("Samsung Galaxy S27 Ultra launch", text="Samsung Galaxy S27 Ultra India launch event", seed=5)]
        plan, _, _ = self.retrieve("phone", photos=items)
        self.assertEqual([a.title for a in plan.photos], ["Samsung Galaxy S27 Ultra launch"])

    def test_n07_footage_of_a_different_match_is_rejected_as_event_footage(self):
        mp4 = make_mp4(self.tmp)
        vids = [video_item("India vs Australia 2019 World Cup highlights", text="India Australia World Cup final highlights")]
        plan, prof, _ = self.retrieve("final", photos=[], videos=vids, mp4=mp4)
        self.assertEqual(plan.videos, [])
        self.assertTrue(any("different event" in r.reason and r.kind == "video" for r in plan.rejected), plan.rejected)
        self.assertEqual(plan.sources["videos"].downloads, [])      # rejected BEFORE anything was downloaded
        self.assertEqual(different_match("India vs Australia 2019", prof.text), "India vs Australia")
        self.assertEqual(different_match("India vs England final celebrations", prof.text), "")

    def test_n07_a_matching_authorised_video_is_accepted_and_chosen(self):
        mp4 = make_mp4(self.tmp)
        vids = [video_item("India beat England in the Women's World Cup final: trophy celebrations",
                           text="Women's World Cup final India England Navi Mumbai celebrations")]
        plan, prof, _ = self.retrieve("final", photos=[], videos=vids, mp4=mp4)
        self.assertEqual(len(plan.videos), 1)
        self.assertEqual(plan.videos[0].role, "event")
        self.assertTrue(plan.videos[0].has_audio)
        self.assertEqual(decide(prof, plan).format, "video")

    def test_n08_copyrighted_publisher_video_without_permission_is_never_downloaded(self):
        media = [{"kind": "video", "url": "https://cdn.publisher.example/final.mp4", "width": 1280, "height": 720,
                  "credit": "Publisher", "licence_hint": ""},
                 {"kind": "photo", "url": "https://cdn.publisher.example/final.jpg", "width": 1600, "height": 900,
                  "credit": "Publisher", "licence_hint": ""}]
        sess = FakeHTTP()
        plan, prof, _ = self.retrieve("final", photos=[], session=sess, media=media)
        self.assertEqual(sess.calls, [])                              # not even robots.txt: nothing was fetched
        self.assertEqual((plan.videos, plan.photos), ([], []))
        reasons = [r.reason for r in plan.rejected]
        self.assertEqual(sum("no documented permission" in x and "not downloaded" in x for x in reasons), 2)
        self.assertNotEqual(decide(prof, plan).format, "video")

    def test_n08_an_openly_licensed_feed_image_is_still_inspected_for_the_right_to_reuse(self):
        media = [{"kind": "photo", "url": "https://cdn.publisher.example/a.jpg", "width": 1600, "height": 900, "credit": "Pub",
                  "licence_hint": "https://creativecommons.org/licenses/by-nc/4.0/"}]
        sess = FakeHTTP()
        plan, _, _ = self.retrieve("final", photos=[], session=sess, media=media)
        self.assertEqual((sess.calls, plan.photos), ([], []))               # NC licence: rejected, not downloaded

    def test_n08_official_source_video_is_used_when_the_domain_is_allow_listed_and_robots_allow(self):
        mp4 = make_mp4(self.tmp)
        arts = articles_for("policy", sources=("PIB (Government of India)", "The Hindu"))
        arts[0]["url"] = "https://pib.gov.in/PressReleasePage.aspx?PRID=1"
        arts[0]["media"] = [{"kind": "video", "url": "https://pib.gov.in/video/gst.mp4", "width": 640, "height": 480,
                             "credit": "PIB", "licence_hint": ""}]
        cl = ranker.build_clusters(arts, NOW)[0]
        prof = profile_for(cl)
        sess = FakeHTTP(mp4)
        plan = MediaRetriever(photo_sources={}, video_sources={}, official_domains=["pib.gov.in"], session=sess,
                              stock_available=False).retrieve(cl, prof, self.tmp / "w", None, NOW)
        self.assertEqual(len(plan.videos), 1, plan.rejected)
        v = plan.videos[0]
        self.assertEqual((v.permission, v.role, v.source), ("official_permitted", "event", "official"))
        self.assertIn("/robots.txt", sess.calls[0])                       # robots.txt consulted before the file

    def test_n08_robots_txt_disallow_is_respected(self):
        arts = articles_for("policy", sources=("PIB (Government of India)", "The Hindu"))
        arts[0]["url"] = "https://pib.gov.in/x"
        arts[0]["media"] = [{"kind": "photo", "url": "https://pib.gov.in/img/a.jpg", "width": 1600, "height": 900,
                             "credit": "PIB", "licence_hint": ""}]
        cl = ranker.build_clusters(arts, NOW)[0]
        sess = FakeHTTP(robots="User-agent: *\nDisallow: /\n")
        media_match._robots.clear()
        plan = MediaRetriever(photo_sources={}, video_sources={}, official_domains=["pib.gov.in"], session=sess,
                              stock_available=False).retrieve(cl, profile_for(cl), self.tmp / "w", None, NOW)
        self.assertEqual(plan.photos, [])
        self.assertTrue(any("robots.txt" in r.reason for r in plan.rejected))
        self.assertEqual([c for c in sess.calls if not c.endswith("robots.txt")], [])

    def test_n09_licensed_photo_keeps_licence_attribution_and_provenance(self):
        it = photo_item("India women celebrate winning the Women's World Cup final against England",
                        text="Harmanpreet Kaur lifts the trophy, Navi Mumbai", license="CC BY 4.0", creator="Jane Doe", seed=7)
        plan, prof, cl = self.retrieve("final", photos=[it])
        self.assertEqual(len(plan.photos), 1, plan.rejected)
        a = plan.photos[0]
        self.assertEqual((a.license, a.permission, a.attribution_required, a.creator, a.role),
                         ("CC BY 4.0", "open_licence", True, "Jane Doe", "event"))
        self.assertEqual((a.original_url, a.page_url), (it["url"], it["page_url"]))
        self.assertEqual(a.story_id, prof.story_id)
        self.assertTrue(a.retrieved_at and a.sha256 and a.rationale)
        self.assertGreaterEqual(a.confidence, config.MEDIA_MIN_CONFIDENCE)
        self.assertNotIn("path", a.to_dict())
        credit = editorial.credits_line([a])
        self.assertIn("Jane Doe (CC BY 4.0)", credit)
        cap = compose_caption(VALID, cl, None, credit)
        self.assertIn("Jane Doe (CC BY 4.0)", cap)
        rec = editorial.build_record(cl, prof, plan, decide(prof, plan))
        self.assertEqual(rec["media"]["photos"][0]["license"], "CC BY 4.0")

    def test_n09_public_domain_needs_no_attribution_but_is_recorded(self):
        it = photo_item("NASA astronaut aboard the International Space Station", text="NASA", license="Public Domain",
                        creator="NASA", seed=8)
        self.assertEqual(provenance.permission_for_licence("Public Domain"), ("public_domain", False))
        self.assertEqual(provenance.permission_for_licence("CC0 1.0"), ("public_domain", False))
        self.assertEqual(provenance.permission_for_licence("CC BY-SA 4.0")[0], "rejected")
        self.assertEqual(provenance.permission_for_licence("CC BY-NC 4.0")[0], "rejected")
        self.assertEqual(provenance.permission_for_licence("")[0], "unverified")
        self.assertEqual(provenance.permission_for_licence("GODL-India"), ("open_licence", True))
        self.assertEqual(it["needs_credit"], False)

    def test_n09_photos_with_unclear_or_restrictive_licences_are_never_downloaded(self):
        title = "India women celebrate winning the Women's World Cup final against England"
        items = [photo_item(title, text="Navi Mumbai", license=lic, seed=10 + i, url=f"https://commons.example/l{i}.jpg")
                 for i, lic in enumerate(("CC BY-NC 4.0", "CC BY-SA 4.0", "All rights reserved", ""))]
        plan, _, _ = self.retrieve("final", photos=items)
        self.assertEqual(plan.photos, [])
        self.assertEqual(plan.sources["photos"].downloads, [])
        self.assertEqual(len(plan.rejected), 4)

    def test_n10_no_media_found_produces_an_honest_editorial_card(self):
        plan, prof, cl = self.retrieve("final", photos=[], stock=True)
        d = decide(prof, plan)
        self.assertEqual((d.format, d.media_use), ("card", "editorial_graphic"))
        self.assertIn("instead of a misleading picture or a generic Reel", d.reason)
        files = photo_post.render_fact_card(VALID, cl, self.tmp, "card", NOW)
        with Image.open(files[0]) as im:
            self.assertEqual(im.size, (1080, 1350))
            self.assertEqual(im.format, "JPEG")

    # --- picture validation
    def test_low_resolution_images_are_rejected(self):
        it = photo_item("India women celebrate winning the Women's World Cup final against England", text="Navi Mumbai",
                        seed=11, width=640, height=420)
        plan, _, _ = self.retrieve("final", photos=[it])
        self.assertEqual(plan.photos, [])
        self.assertTrue(any("resolution" in r.reason for r in plan.rejected), plan.rejected)

    def test_a_download_that_is_not_an_image_is_rejected_cleanly(self):
        title = "India women celebrate winning the Women's World Cup final against England"
        items = [photo_item(title, text="Navi Mumbai", seed=12, bad="html", url="https://commons.example/e1.jpg"),
                 photo_item(title + " trophy", text="Navi Mumbai", seed=13, bad="http", url="https://commons.example/e2.jpg")]
        plan, _, _ = self.retrieve("final", photos=items)
        self.assertEqual(plan.photos, [])
        self.assertEqual(len(plan.rejected), 2)

    def test_duplicate_pictures_are_detected(self):
        title = "India women celebrate winning the Women's World Cup final against England"
        items = [photo_item(title, text="Navi Mumbai", seed=20, url="https://commons.example/d1.jpg"),
                 photo_item(title + " again", text="Navi Mumbai", seed=20, url="https://commons.example/d2.jpg"),
                 photo_item(title + " trophy", text="Navi Mumbai", seed=21, url="https://commons.example/d3.jpg")]
        plan, _, _ = self.retrieve("final", photos=items)
        self.assertEqual(len(plan.photos), 2)
        self.assertTrue(any("duplicate" in r.reason for r in plan.rejected))

    def test_old_photo_is_never_labelled_as_the_new_event(self):
        it = photo_item("India women lift the Women's World Cup trophy 2017 England final", text="Women's World Cup India England",
                        date="2017-07-23", seed=30)
        plan, _, _ = self.retrieve("final", photos=[it])
        self.assertEqual([a.role for a in plan.photos], ["subject"])
        with mock.patch.object(config, "ALLOW_FILE_PHOTOS", False):
            plan2, _, _ = self.retrieve("final", photos=[it])
        self.assertEqual(plan2.photos, [])
        self.assertTrue(any("only a file photo" in r.reason for r in plan2.rejected))

    def test_low_confidence_is_rejected_with_the_threshold_in_the_reason(self):
        it = photo_item("India England Women's World Cup photo", text="", date="", seed=31)      # right subject, nothing else
        plan, _, _ = self.retrieve("final", photos=[it])
        self.assertEqual([a.role for a in plan.photos], ["subject"])
        self.assertLess(plan.photos[0].confidence, 0.9)
        with mock.patch.object(config, "MEDIA_MIN_CONFIDENCE", 0.9):
            plan, _, _ = self.retrieve("final", photos=[it])
        self.assertEqual(plan.photos, [])
        self.assertTrue(any("below threshold" in r.reason for r in plan.rejected), plan.rejected)

    def test_story_queries_use_entities_not_the_category(self):
        cl = cluster_for("final")
        qs = MediaRetriever(photo_sources={}, video_sources={}, official_domains=[], stock_available=False).queries_for(cl, profile_for(cl))
        joined = " ".join(qs).lower()
        self.assertIn("harmanpreet kaur", joined)
        self.assertIn("women's world cup", joined)
        self.assertTrue(all(q.lower() not in {"cricket", "sports", "politics", "india news", "technology"} for q in qs))

    def test_provider_failure_is_recorded_and_does_not_stop_retrieval(self):
        class Boom(FakePhotos):
            def candidates(self, q, c=None):
                raise requests.ConnectionError("down")
        cl = cluster_for("final")
        r = MediaRetriever(photo_sources={"commons": Boom([])}, video_sources={}, official_domains=[], stock_available=False)
        plan = r.retrieve(cl, profile_for(cl), self.tmp / "w", None, NOW)
        self.assertEqual(plan.photos, [])
        self.assertTrue(plan.providers["commons"].startswith("error"))


# =====================================================================================================================
# N11-N14: collection, clustering, trends, caption validation
# =====================================================================================================================
class TestDiscoveryAndRanking(Env):
    def two_outlet_articles(self):
        a = {"id": "a", "title": "Rain disrupts flights across Mumbai as airport suspends runway operations", "source": "BBC News",
             "summary": "Heavy rain forced Mumbai airport to suspend runway operations for two hours on Monday. Dozens of flights were delayed.",
             "url": "https://news.example/a", "category": "News", "published": (NOW - timedelta(minutes=40)).isoformat()}
        b = {"id": "b", "title": "Mumbai airport suspends runway operations as heavy rain disrupts flights", "source": "NDTV News",
             "summary": "Mumbai airport suspended runway operations for two hours on Monday because of heavy rain. Dozens of flights were delayed.",
             "url": "https://other.example/b", "category": "News", "published": (NOW - timedelta(minutes=25)).isoformat()}
        return a, b

    def test_n11_two_outlets_on_the_same_event_form_one_story(self):
        a, b = self.two_outlet_articles()
        clusters = ranker.build_clusters([a, b], NOW)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["sources"], ["BBC News", "NDTV News"])
        self.assertEqual(clusters[0]["signals"]["independent_sources"], 2)

    def test_n11_a_later_duplicate_report_is_not_published_again(self):
        a, b = self.two_outlet_articles()
        history_titles = [a["title"]]
        later_cluster = ranker.build_clusters([b], NOW)[0]
        self.assertTrue(similar_to_history(later_cluster, history_titles))
        flat = [{"url": a["url"], "title": a["title"]}]
        self.assertEqual(filter_published([a], flat), [])

    def test_n12_a_corroborated_fresh_story_outranks_a_single_stale_report(self):
        fresh = [{**x, "id": f"f{i}", "source": s, "title": "Government announces new national semiconductor mission with major funding",
                  "summary": "The government announced a semiconductor mission on Monday with major funding for new fabrication plants. "
                             "Ministers said the programme will create jobs across several states.",
                  "url": f"https://x.example/f{i}", "published": (NOW - timedelta(minutes=20 + i * 5)).isoformat()}
                 for i, (x, s) in enumerate(zip([self.two_outlet_articles()[0]] * 4, ["BBC News", "NDTV News", "The Hindu", "Al Jazeera"]))]
        stale = [{**self.two_outlet_articles()[0], "id": "st", "title": "Local museum extends opening hours for summer visitors this year",
                  "summary": "A local museum will stay open longer this summer.", "source": "Hindustan Times",
                  "url": "https://x.example/stale", "published": (NOW - timedelta(hours=20)).isoformat()}]
        clusters = ranker.build_clusters(fresh + stale, NOW)
        top = clusters[0]
        self.assertEqual(top["signals"]["independent_sources"], 4)
        self.assertGreater(top["breakdown"]["confirmation"], clusters[-1]["breakdown"]["confirmation"])
        self.assertGreater(top["breakdown"]["freshness"], clusters[-1]["breakdown"]["freshness"])
        self.assertGreater(top["score"], clusters[-1]["score"])
        self.assertIn("4 outlet(s)", top["why"])
        self.assertTrue(top["urgent"] or top["breaking"])

    def test_wire_copy_from_several_outlets_is_corroboration_not_a_duplicate(self):
        arts = articles_for("rbi", sources=("BBC News", "The Hindu", "NDTV News"))
        from core.dedupe import dedupe_batch
        self.assertEqual(len(dedupe_batch(arts)), 3)                          # identical headline, three different outlets
        self.assertEqual(len(dedupe_batch(arts + [dict(arts[0], id="again")])), 3)           # same outlet + URL twice: dropped
        same_outlet = dict(arts[0], url="https://news.example/rbi/other-url")
        self.assertEqual(len(dedupe_batch(arts + [same_outlet])), 3)                          # same outlet, same headline: dropped
        c = ranker.build_clusters(dedupe_batch(arts), NOW)[0]
        self.assertEqual(c["signals"]["independent_sources"], 3)
        self.assertGreater(c["breakdown"]["confirmation"], 60)

    def test_n12_urgent_stories_override_category_balancing(self):
        arts = []
        for i, s in enumerate(["BBC News", "NDTV News", "The Hindu", "Al Jazeera"]):
            arts.append({"id": f"u{i}", "title": "Parliament passes landmark election reform bill after marathon debate",
                         "summary": "Parliament passed the election reform bill late on Monday after a marathon debate. Opposition parties walked out.",
                         "source": s, "url": f"https://x.example/u{i}", "category": "News",
                         "published": (NOW - timedelta(minutes=30)).isoformat()})
        arts.append({"id": "t", "title": "Startup unveils compact laptop with new efficient processor chip design",
                     "summary": "A startup unveiled a compact laptop on Monday. The laptop uses a new processor chip.",
                     "source": "TechCrunch", "url": "https://x.example/t", "category": "Technology",
                     "published": (NOW - timedelta(minutes=30)).isoformat()})
        clusters = ranker.build_clusters(arts, NOW)
        recent = ["News", "News", "News"]
        picks = ranker.ordered_candidates(clusters, recent, {"News": 1, "Technology": 100}, min_score=0, limit=2, skip_sensitive=False)
        urgent = [c for c in clusters if c.get("urgent")]
        if urgent:
            self.assertTrue(picks[0].get("urgent"))

    def test_n13_google_trends_unavailable_pipeline_continues_with_other_signals(self):
        def fail(url, **kw):
            raise requests.ConnectionError("blocked")
        sess = mock.Mock(get=fail)
        rep = trends_mod.fetch_trend_report(session=sess, providers=["google", "wikipedia"], cache_path=self.tmp / "tc.json")
        self.assertEqual(rep.status, "unavailable")
        self.assertEqual({v["status"] for v in rep.providers.values()}, {"unavailable"})
        clusters = ranker.build_clusters(articles_for("rbi"), NOW, rep)
        c = clusters[0]
        self.assertEqual(c["signals"]["trend_status"], "unavailable")
        self.assertIsNone(c["signals"]["trend_volume"])                     # nothing invented
        self.assertIn("absence is unknown", c["why"])
        self.assertGreater(c["score"], 0)

    def test_n13_unavailable_is_not_the_same_as_empty(self):
        ok_empty = mock.Mock(get=lambda url, **kw: Resp(200, {"items": [{"articles": []}]}))
        rep = trends_mod.fetch_trend_report(session=ok_empty, providers=["wikipedia"], cache_path=self.tmp / "tc.json")
        self.assertEqual(rep.status, "empty")
        c = ranker.build_clusters(articles_for("rbi"), NOW, rep)[0]
        self.assertNotIn("absence is unknown", c["why"])
        self.assertIn("no matching trend", c["why"])

    def test_trend_volume_is_only_what_the_provider_reported(self):
        t = trends_mod.Trend("repo rate", 0, [])
        t.provider = "google"
        c = ranker.build_clusters(articles_for("rbi"), NOW, [t])[0]
        self.assertEqual(c["trend"], "repo rate")
        self.assertIsNone(c["signals"]["trend_volume"])                     # 0/unknown stays unknown
        self.assertIn("volume not reported", c["why"])

    def test_important_but_not_trending_is_labelled_differently(self):
        c = ranker.build_clusters(articles_for("rbi", sources=("BBC News",)), NOW)[0]
        self.assertNotEqual(c["kind"], "trending")                          # no trend match, <3 articles in 3h

    def test_n14_unsupported_number_or_quote_in_the_copy_is_rejected(self):
        src = STORIES["rbi"]["summary"]
        bad_num = dict(VALID, hook="RBI keeps the repo rate unchanged at 7.25 per cent today")
        with self.assertRaises(ContentError) as cm:
            validate_content(bad_num, src, "Business")
        self.assertIn("numbers not found", str(cm.exception))
        bad_quote = dict(VALID, caption=VALID["caption"] + ' The governor said "we will cut rates aggressively in December".')
        with self.assertRaises(ContentError) as cm2:
            validate_content(bad_quote, src, "Business")
        self.assertIn("quotation", str(cm2.exception))

    def test_n14_writer_retries_until_the_copy_is_grounded(self):
        calls = []

        def ai(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return json.dumps(dict(VALID, hook="Apple unveils a chip with 48 hours of battery life"))
            return json.dumps(VALID)
        w = AIWriter(generate_json=ai, cache_path=self.tmp / "ai.json", sleep=lambda s: None)
        cl = {"fingerprint": "fpx", "category": "Technology", "articles": articles_for("phone")[:1],
              "lead": articles_for("phone")[0], "sources": ["BBC News"], "breaking": False, "score": 60, "age_hours": 1}
        cl["articles"][0]["summary"] = ("Apple announced a new processor on Tuesday. The chip targets thin laptops and promises longer "
                                        "battery life for students and remote workers. Pricing starts later this year.")
        content, mode = w.write(cl)
        self.assertEqual(mode, "ai")
        self.assertNotIn("48", json.dumps(content))
        self.assertEqual(len(calls), 2)


# =====================================================================================================================
# N15-N18: pipeline safety, recorded format, workflow
# =====================================================================================================================
class FakeRetriever:
    """Returns a prepared MediaPlan whose files live in the pipeline's working directory."""

    def __init__(self, build):
        self.build, self.calls = build, 0

    def retrieve(self, cand, prof, work_dir, content, now):
        self.calls += 1
        return self.build(Path(work_dir))


def photos_plan(n=1, *, role="event", video=False, conf=0.9, clips=0):
    def build(work: Path) -> MediaPlan:
        photos = [asset(title=f"Photo {i}", role=role, conf=conf, path=str(make_jpg(work / f"p{i}.jpg", 100 + i)))
                  for i in range(n)]
        videos = []
        if video:
            mp4 = make_mp4(work, "src.mp4")
            videos = [asset("video", role="event", duration=4, audio=True, permission="official_permitted", path=str(mp4),
                            width=640, height=480)]
        return MediaPlan("s", photos=photos, videos=videos, illustrative_clips=clips)
    return build


class EditorialPipeline(PipelineFixture, Env):
    def setUp(self):
        Env.setUp(self)
        self.reel_calls = []

    def reel_maker(self, fail=False):
        calls = self.reel_calls

        def make(content, cand, out_path, music_path=None, **kw):
            calls.append(kw)
            if fail:
                raise video_renderer.VideoError("render exploded")
            subprocess.run([video_renderer.find_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=s=320x568:r=30:d=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)], check=True)
            from core import reel_studio
            return reel_studio.ReelResult(Path(out_path), kw.get("voiceover", False), "photos", 8.0)
        return make

    def make(self, key, plan_build, *, ig=None, reel_fail=False):
        ai = lambda p: json.dumps(REEL_VALID if "VOICEOVER" in p else VALID)         # noqa: E731
        p = self.build(articles=articles_for(key, sources=("BBC News", "The Hindu", "NDTV News")), ig=ig, ai=ai)
        p._retriever = FakeRetriever(plan_build)
        p.reel_maker = self.reel_maker(reel_fail)
        p.verify_public = lambda url, kind="image": True
        return p

    def decisions(self):
        return editorial.read_decisions(self.tmp, 20)

    def creates(self):
        return [c for c in self.ig_session.calls if c[0] == "POST" and c[1] == "U/media" and not c[2].get("is_carousel_item")]

    # ---- N17 ------------------------------------------------------------------------------------------------------
    def test_n17_each_format_is_published_as_its_own_media_type_and_recorded_that_way(self):
        cases = [("final", photos_plan(1), "photo", lambda c: "media_type" not in c[2] and "image_url" in c[2], ["image/jpeg"]),
                 ("final", lambda w: MediaPlan("s", illustrative_clips=0), "card", lambda c: "image_url" in c[2], ["image/jpeg"]),
                 ("final", photos_plan(video=True), "video", lambda c: c[2].get("media_type") == "REELS", ["video/mp4"]),
                 ("explainer", photos_plan(1), "reel", lambda c: c[2].get("media_type") == "REELS", ["video/mp4"]),
                 ("multi", photos_plan(3), "carousel", lambda c: c[2].get("media_type") == "CAROUSEL", None)]
        for key, build, fmt, media_check, kinds in cases:
            with self.subTest(fmt=fmt):
                for f in self.tmp.glob("*.json*"):
                    f.unlink()
                p = self.make(key, build)
                res = p.run_cycle(force=True)
                self.assertEqual((res["status"], res["format"]), ("published", fmt), res)
                self.assertEqual(p.load_history()[-1]["format"], fmt)
                create = self.creates()[-1]
                self.assertTrue(media_check(create), create)
                dec = self.decisions()[-1]
                self.assertEqual(dec["decision"]["format"], fmt)
                self.assertEqual(dec["outcome"]["final_format"], fmt)
                self.assertTrue(dec["outcome"]["published"])
                if kinds:
                    self.assertEqual(dec["outcome"]["uploaded_media_types"], kinds)
                else:
                    self.assertTrue(all(k == "image/jpeg" for k in dec["outcome"]["uploaded_media_types"]))
                self.assertGreaterEqual(len(dec["decision"]["trail"]), 2)      # every rule it evaluated is recorded

    def test_n17_the_story_profiles_used_above_really_are_what_the_tests_assume(self):
        self.assertTrue(profile_for(cluster_for("final")).simple_update)
        pr = profile_for(cluster_for("explainer", sources=("BBC News", "The Hindu", "NDTV News")))
        self.assertGreaterEqual(pr.narration_value, config.REEL_NARRATION_MIN)
        pm = profile_for(cluster_for("multi", sources=("BBC News", "The Hindu", "NDTV News")))
        self.assertFalse(pm.simple_update)
        self.assertGreaterEqual(pm.fact_points, 3)

    def test_n17_original_video_keeps_its_sound_and_is_a_valid_reel_file(self):
        p = self.make("final", photos_plan(video=True))
        res = p.run_cycle(dry_run=True, force=True)
        self.assertEqual((res["status"], res["format"]), ("dry_run", "video"), res)
        mp4 = next(Path(f) for f in res["files"] if f.endswith(".mp4"))
        info = video_renderer.probe(mp4)
        self.assertEqual((info["size"], info["video"]), ((1080, 1920), "h264"))
        self.assertTrue(info["audio"])                                   # the original sound survived: no AI voice added
        self.assertEqual(self.reel_calls, [])                            # the Reel renderer / TTS was never used
        self.assertIn("Original footage", (self.tmp / "preview").glob("*_caption.txt").__next__().read_text(encoding="utf-8"))

    # ---- N15 ------------------------------------------------------------------------------------------------------
    def test_n15_a_failing_preferred_format_falls_back_and_the_recorded_format_is_the_real_one(self):
        p = self.make("final", photos_plan(1))
        with mock.patch("core.pipeline.render_photo_post", side_effect=ValueError("render blew up")):
            res = p.run_cycle(force=True)
        self.assertEqual((res["status"], res["format"]), ("published", "card"), res)
        self.assertEqual(p.load_history()[0]["format"], "card")
        self.assertEqual(self.decisions()[-1]["outcome"]["rendered_format"], "card")

    def test_n15_a_failing_reel_falls_back_to_a_still_format_never_a_fake_reel(self):
        p = self.make("explainer", photos_plan(1), reel_fail=True)
        res = p.run_cycle(force=True)
        self.assertEqual((res["status"], res["format"]), ("published", "photo"), res)
        self.assertEqual(self.creates()[-1][2].get("media_type"), None)

    def test_n15_media_host_failure_is_not_recorded_as_a_publication(self):
        p = self.make("final", photos_plan(1))
        p._host = mock.Mock(upload=mock.Mock(side_effect=media_host.MediaHostError("cloudinary 500")), delete=mock.Mock())
        res = p.run_cycle(force=True)
        self.assertEqual(res["status"], "failed")
        self.assertEqual(p.load_history(), [])
        self.assertEqual(self.ig_session.published, 0)
        self.assertFalse(self.decisions()[-1]["outcome"]["published"])
        self.assertEqual(p.health.state["consecutive_failures"], 1)

    def test_n15_unreachable_hosted_media_stops_before_instagram_is_called(self):
        p = self.make("final", photos_plan(1))
        p.verify_public = lambda url, kind="image": False
        res = p.run_cycle(force=True)
        self.assertEqual(res["status"], "failed")
        self.assertEqual((self.ig_session.published, p.load_history(), self.creates()), (0, [], []))

    def test_n15_when_every_format_fails_nothing_is_recorded(self):
        p = self.make("final", photos_plan(1))
        with mock.patch("core.pipeline.render_photo_post", side_effect=OSError("disk full")), \
             mock.patch("core.pipeline.render_fact_card", side_effect=OSError("disk full")):
            res = p.run_cycle(force=True)
        self.assertEqual(res["status"], "failed")
        self.assertEqual((p.load_history(), self.ig_session.published), ([], 0))

    # ---- N16 ------------------------------------------------------------------------------------------------------
    def test_n16_ambiguous_publish_result_never_leads_to_a_duplicate_post(self):
        s = FakeIG()
        s.overrides["POST:U/media_publish"] = [requests.Timeout()]
        p = self.make("final", photos_plan(1), ig=s)
        res = p.run_cycle(force=True)
        self.assertEqual(res["status"], "failed")
        self.assertIn("ambiguous", res["error"])
        self.assertEqual(p.load_history(), [])                              # not claimed as published ...
        pending = storage.read_json(self.tmp / "pending_publish.json", [])
        self.assertEqual(len(pending), 1)                                   # ... but remembered for reconciliation
        self.assertTrue(self.decisions()[-1]["outcome"]["ambiguous"])
        # next run: the story is excluded; Instagram never receives a second publish
        res2 = self.make("final", photos_plan(1), ig=s).run_cycle(force=True)
        self.assertEqual(res2["status"], "idle")
        self.assertEqual(s.published, 0)

    def test_n16_a_later_run_reconciles_the_post_that_actually_went_live(self):
        s = FakeIG()
        s.overrides["POST:U/media_publish"] = [requests.Timeout()]
        p = self.make("final", photos_plan(1), ig=s)
        p.run_cycle(force=True)
        caption = s.last_caption
        s.overrides["GET:U/media"] = [Resp(200, {"data": [{"id": "m77", "caption": caption, "permalink": "https://instagram.com/p/live/"}]})] * 3
        p2 = self.make("final", photos_plan(1), ig=s)
        res = p2.run_cycle(force=True)
        hist = p2.load_history()
        self.assertEqual([h["media_id"] for h in hist if h.get("reconciled")], ["m77"])
        self.assertEqual(s.published, 0)
        self.assertEqual(storage.read_json(self.tmp / "pending_publish.json", []), [])
        self.assertEqual(res["status"], "idle")

    # ---- hold / sensitive ---------------------------------------------------------------------------------------
    def test_a_sensitive_story_with_a_single_outlet_is_held_not_published(self):
        arts = [{"id": "q", "title": "Earthquake of magnitude 6 strikes coastal region, buildings collapse in several towns",
                 "summary": "An earthquake struck the coastal region on Monday and buildings collapsed in several towns. Rescue teams are searching.",
                 "source": "BBC News", "url": "https://news.example/q", "category": "News",
                 "published": (NOW - timedelta(minutes=20)).isoformat()}]
        p = self.build(articles=arts)
        p._retriever = FakeRetriever(photos_plan(1))
        res = p.run_cycle(force=True)
        self.assertEqual(res["status"], "idle")
        self.assertIn("held", res["reason"])
        self.assertEqual((p.load_history(), self.ig_session.published), ([], 0))
        self.assertTrue(self.decisions()[-1]["outcome"]["held"])

    def test_conflicting_election_figures_are_held(self):
        cl = cluster_for("rbi")
        pr = profile_for(cl)
        pr.sensitive, pr.conflicting_figures = ["election"], ["seats: 120, 135"]
        self.assertIn("different figures", story.hold_reason(pr))

    def test_writer_guidance_attributes_allegations(self):
        pr = profile(allegation=True, sensitive=["crime"])
        g = story.writer_guidance(pr)
        self.assertIn("allegations", g)
        self.assertIn("Sensitive topic", g)

    # ---- dry run explains itself --------------------------------------------------------------------------------
    def test_dry_run_returns_an_inspectable_explanation_and_publishes_nothing(self):
        p = self.make("final", photos_plan(1))
        res = p.run_cycle(dry_run=True, force=True)
        self.assertEqual(res["status"], "dry_run")
        report = res["report"]
        for needle in ("STORY", "RANKING", "DECISION", "Selected single-photo post", "accepted photo", "MEDIA USE"):
            self.assertIn(needle, report)
        files = list((self.tmp / "preview").glob("*"))
        self.assertTrue(any(f.name.endswith("_decision.json") for f in files))
        rec = json.loads(next(f for f in files if f.name.endswith("_decision.json")).read_text(encoding="utf-8"))
        for key in ("story", "trend_signals", "corroboration", "analysis", "media", "decision", "explanation", "outcome"):
            self.assertIn(key, rec)
        self.assertEqual((self.ig_session.published, p.load_history()), (0, []))

    def test_decision_records_never_contain_secrets(self):
        p = self.make("final", photos_plan(1))
        p.run_cycle(force=True)
        blob = (self.tmp / "decisions.jsonl").read_text(encoding="utf-8") + json.dumps(p.load_history())
        self.assertNotIn("TOKEN_SECRET", blob)

    def test_rejections_are_logged_with_reasons(self):
        plan, _, _ = self.retrieve("policy", photos=[photo_item("Smriti Irani addresses a rally", text="Nirmala Sitharaman", seed=1)])
        cl = cluster_for("policy")
        rec = editorial.build_record(cl, profile_for(cl), plan, decide(profile_for(cl), plan))
        self.assertIn("wrong person", editorial.format_report(rec))


class TestWorkflowAndConfig(unittest.TestCase):
    WF = ROOT / ".github" / "workflows" / "instagram_agent.yml"

    def load(self):
        return yaml.safe_load(self.WF.read_text(encoding="utf-8"))

    def test_n18_workflow_runs_the_agent_and_persists_state(self):
        text = self.WF.read_text(encoding="utf-8")
        wf = self.load()
        trig = wf.get("on") or wf.get(True)
        self.assertIn("schedule", trig)
        self.assertIn("workflow_dispatch", trig)
        self.assertEqual(wf["concurrency"]["group"], "instagram-agent")
        self.assertFalse(wf["concurrency"]["cancel-in-progress"])         # never cancel a run that may be mid-publish
        job = wf["jobs"]["run"]
        self.assertLessEqual(job["timeout-minutes"], 30)
        self.assertIn("python main.py run", text)
        for state in ("data/history.json", "data/schedule_state.json", "data/attempts.json", "data/health_state.json",
                      "data/pending_publish.json", "data/decisions.jsonl", "data/style_history.json"):
            self.assertIn(state, text, state)
        self.assertEqual(wf["permissions"]["contents"], "write")
        self.assertIn("always()", text)

    def test_n18_workflow_passes_every_secret_the_agent_reads_and_the_new_editorial_settings(self):
        text = self.WF.read_text(encoding="utf-8")
        for name in ("IG_USER_ID", "IG_ACCESS_TOKEN", "GEMINI_API_KEY", "CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY",
                     "CLOUDINARY_API_SECRET", "FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN", "FB_MODE", "FORMAT_POLICY",
                     "MEDIA_MIN_CONFIDENCE", "REEL_VOICEOVER", "TREND_PROVIDERS", "SOURCES_FILE", "OFFICIAL_MEDIA_DOMAINS",
                     "PEXELS_API_KEY", "PIXABAY_API_KEY"):
            self.assertIn(name, text, name)

    def test_n18_workflow_manual_formats_match_what_the_cli_accepts(self):
        wf = self.load()
        trig = wf.get("on") or wf.get(True)
        opts = trig["workflow_dispatch"]["inputs"]["format"]["options"]
        sub = subprocess.run(["python", str(ROOT / "main.py"), "run", "--help"], capture_output=True, text=True, cwd=ROOT)
        for o in opts:
            if o != "auto":
                self.assertIn(o, sub.stdout, f"--format {o} is offered by the workflow but not by main.py")

    def test_every_config_variable_is_documented_in_env_example_and_readme(self):
        cfg = (ROOT / "core" / "config.py").read_text(encoding="utf-8")
        names = sorted(set(re.findall(r'_[sifb]\("([A-Z0-9_]+)"', cfg)))
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        missing_env = [n for n in names if n not in env]
        missing_readme = [n for n in names if n not in readme and n not in env]
        self.assertEqual(missing_env, [], f"missing from .env.example: {missing_env}")
        self.assertEqual(missing_readme, [])

    def test_no_secrets_in_the_repository_files(self):
        pat = re.compile(r"(AIza[0-9A-Za-z_\-]{30,}|IGAA[0-9A-Za-z]{40,}|EAA[0-9A-Za-z]{60,}|ghp_[0-9A-Za-z]{30,})")
        for f in list(ROOT.glob("*.py")) + list((ROOT / "core").glob("*.py")) + [ROOT / ".env.example", ROOT / "README.md", self.WF]:
            self.assertIsNone(pat.search(f.read_text(encoding="utf-8")), f.name)


class TestSourceConfiguration(Env):
    def test_sources_file_adds_disables_and_sets_quality(self):
        f = self.tmp / "sources.json"
        f.write_text(json.dumps({"add": [{"category": "News", "name": "Sample Wire", "url": "https://wire.example/rss", "quality": 0.97}],
                                 "disable": ["Al Jazeera"], "quality": {"BBC News": 0.99},
                                 "official_media_domains": ["pib.gov.in"]}), encoding="utf-8")
        from core import feeds
        old = dict(feeds.SOURCE_QUALITY)
        self.addCleanup(lambda: (feeds.SOURCE_QUALITY.clear(), feeds.SOURCE_QUALITY.update(old)))
        with mock.patch.object(config, "SOURCES_FILE", str(f)):
            merged = feeds.all_feeds("")
        names = {x["name"] for items in merged.values() for x in items}
        self.assertIn("Sample Wire", names)
        self.assertNotIn("Al Jazeera", names)
        self.assertEqual(feeds.SOURCE_QUALITY["Sample Wire"], 0.97)

    def test_a_broken_sources_file_is_ignored_not_fatal(self):
        f = self.tmp / "bad.json"
        f.write_text("{not json", encoding="utf-8")
        from core import feeds
        with mock.patch.object(config, "SOURCES_FILE", str(f)):
            self.assertGreater(len(feeds.all_feeds("")), 3)

    def test_feed_media_metadata_is_extracted_but_marked_as_unverified(self):
        entry = {"title": "A long enough headline for the feed item here", "link": "https://x.example/1", "summary": "s",
                 "media_content": [{"url": "https://cdn.example/a.jpg", "medium": "image", "width": "1200", "height": "800"},
                                   {"url": "https://cdn.example/v.mp4", "type": "video/mp4"}],
                 "published_parsed": NOW.timetuple()}
        art = collector.parse_entry(entry, "Example", "News")
        kinds = {m["kind"] for m in art["media"]}
        self.assertEqual(kinds, {"photo", "video"})
        self.assertTrue(all(not m.get("licence_hint") for m in art["media"]))

    def test_collection_report_distinguishes_failed_feeds(self):
        def get(url, **kw):
            if "bad" in url:
                raise requests.ConnectionError("x")
            return mock.Mock(content=b"<rss><channel></channel></rss>", raise_for_status=lambda: None)
        feeds_cfg = {"News": [{"name": "Good", "url": "https://good.example/rss"}, {"name": "Bad", "url": "https://bad.example/rss"}]}
        collector.collect_news(feeds=feeds_cfg, session=mock.Mock(get=get), now=NOW)
        self.assertEqual(collector.LAST_REPORT["feeds_failed"], ["Bad"])
        self.assertEqual(collector.LAST_REPORT["feeds_ok"], 1)


if __name__ == "__main__":
    unittest.main()
