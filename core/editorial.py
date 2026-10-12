"""Editorial decision engine: which format does THIS story deserve, given the media we may legally and honestly use?

The engine never looks at CONTENT_FORMATS weights and never prefers Reels because the Reel renderer exists.
Rules, in priority order (each is a small, testable function; the trail of every rule it evaluated is recorded):

  H  hold          the story is not safe to publish yet (thin sourcing, conflicting figures, low confidence)
  V  video         an AUTHORISED video that really shows this event exists            -> original video post
  C  carousel      several distinct, relevant photos AND several facts, not a simple update
  P  photo         a relevant real photo exists and narration adds little              -> single photo post
  R  reel          narration/visual explanation clearly helps AND authentic (or properly labelled illustrative) visuals
  F  card          nothing authentic to show: a clean original fact card, never a misleading picture or a generic Reel

What the output may claim (`media_use`)
  event_media            the media was documented as showing this event
  file_photo             right person/place/product, not shown to be this event: published with a "File photo" label
  licensed_illustrative  licensed generic footage, labelled as illustrative, never presented as the event
  editorial_graphic      typographic card built from verified facts
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .provenance import MediaAsset, MediaPlan

log = logging.getLogger("editorial")
FORMATS = ("video", "photo", "carousel", "reel", "card")


@dataclass
class Decision:
    format: str                         # video | photo | carousel | reel | card | hold
    reason: str
    trail: list[str] = field(default_factory=list)
    media_use: str = "editorial_graphic"
    photos: list[MediaAsset] = field(default_factory=list)      # photos the chosen format will show (best first)
    video: MediaAsset | None = None
    voiceover: bool = False
    illustrative_footage: bool = False
    forced: bool = False
    policy: str = "editorial"

    @property
    def lead_photo(self) -> MediaAsset | None:
        return self.photos[0] if self.photos else None

    def explanation(self) -> str:
        return f"Selected {LABEL.get(self.format, self.format)}: {self.reason}"

    def to_dict(self) -> dict:
        return {"format": self.format, "reason": self.reason, "trail": self.trail, "media_use": self.media_use,
                "voiceover": self.voiceover, "illustrative_footage": self.illustrative_footage, "forced": self.forced,
                "policy": self.policy, "photos": [a.to_dict() for a in self.photos],
                "video": self.video.to_dict() if self.video else None}


LABEL = {"video": "original video post", "photo": "single-photo post", "carousel": "carousel", "reel": "Reel",
         "card": "editorial fact card", "hold": "HOLD (do not publish)"}


def _best_video(plan: MediaPlan) -> MediaAsset | None:
    for v in plan.videos:
        if (v.role == "event" and v.permission in ("public_domain", "open_licence", "official_permitted", "owned")
                and config.VIDEO_MIN_SECONDS <= v.duration and v.width and v.height):
            return v
    return None


def _media_use(photos: list[MediaAsset], video: MediaAsset | None = None) -> str:
    if video is not None:
        return "event_media"
    if not photos:
        return "editorial_graphic"
    return "event_media" if all(p.role == "event" for p in photos) else "file_photo"


def decide(profile, plan: MediaPlan, *, encoder_ok: bool = True, tts_ok: bool = True, forced: str | None = None,
           category: str | None = None, avoid: set[str] | None = None) -> Decision:
    """Pick the format for one story. Pure function of (story analysis, media found, capabilities, config)."""
    category = category or profile.category
    avoid = set(avoid or ())
    t: list[str] = []
    if avoid:
        t.append("fallback: could not build " + "/".join(sorted(avoid)) + " (render/encode failure)")
    photos = list(plan.photos)
    event_photos = plan.event_photos
    video = _best_video(plan)
    narr, facts = profile.narration_value, profile.fact_points
    illustrative_ok = (plan.illustrative_clips > 0 and category in config.ILLUSTRATIVE_CATEGORIES
                       and not profile.event_story)

    # H ---------------------------------------------------------------- hold
    if profile.hold_reason:
        t.append("H: " + profile.hold_reason)
        return Decision("hold", profile.hold_reason, t, "none")
    t.append("H: story is safe to publish (confidence %.2f, %d independent source(s))"
             % (profile.confidence, profile.independent_sources))

    def finish(fmt: str, reason: str, *, use_photos=None, vid=None, voice=False, illus=False) -> Decision:
        use_photos = photos[:10] if use_photos is None else use_photos
        use = "licensed_illustrative" if (fmt == "reel" and illus and not use_photos) else _media_use(use_photos, vid)
        if fmt in ("card", "carousel") and not use_photos:
            use = "editorial_graphic"
        return Decision(fmt, reason, t, use, use_photos, vid, voice, illus, forced=bool(forced))

    def reel_voice() -> bool:
        if config.REEL_VOICEOVER == "never" or not tts_ok:
            return False
        return True if config.REEL_VOICEOVER == "always" else narr >= config.REEL_NARRATION_MIN

    # operator override (--format): respected when feasible, and recorded as such ---------------
    if forced and forced not in avoid:
        t.append(f"operator forced --format {forced}")
        if forced == "video" and video and encoder_ok:
            return finish("video", "operator forced an original video and an authorised event video exists", vid=video)
        if forced == "photo" and photos:
            return finish("photo", "operator forced a photo post", use_photos=photos[:1])
        if forced == "card":
            return finish("card", "operator forced a fact card", use_photos=[])
        if forced == "carousel":
            return finish("carousel", "operator forced a carousel", use_photos=photos[:4])
        if forced == "reel" and encoder_ok:
            if not photos and not illustrative_ok and profile.event_story:
                t.append("forced Reel has no authentic visuals for an event story: building it with an animated, "
                         "clearly non-photographic background")
            return finish("reel", "operator forced a Reel", use_photos=photos[:4], voice=reel_voice(),
                          illus=illustrative_ok)
        t.append(f"forced format '{forced}' is not possible with the media available: deciding automatically")

    # V ---------------------------------------------------------------- authorised original video
    if video and encoder_ok and "video" not in avoid:
        t.append(f"V: authorised event video available ({video.permission}, confidence {video.confidence:.2f}, "
                 f"{video.duration:.0f}s)")
        return finish("video", "authorised event video available" + ("; original audio is preserved" if video.has_audio
                      else "; it has no audio, so none is invented") + "; narration unnecessary",
                      use_photos=[], vid=video)
    t.append("V: no authorised video that shows this event" if not plan.videos else
             "V: video found but not usable as event footage (role/duration/permission)")

    # C ---------------------------------------------------------------- carousel
    distinct = photos
    if "carousel" in avoid:
        pass
    elif len(distinct) >= config.CAROUSEL_MIN_PHOTOS and facts >= 3 and not profile.simple_update:
        t.append(f"C: {len(distinct)} distinct relevant photos and {facts} separate facts improve understanding")
        return finish("carousel", f"{len(distinct)} relevant photos plus {facts} distinct facts form a meaningful "
                                  f"sequence", use_photos=distinct[:4])
    elif not photos and facts >= 4 and not profile.simple_update and narr < config.REEL_NARRATION_MIN:
        t.append(f"C: no photo, but {facts} distinct facts form a structured sequence")
        return finish("carousel", f"structured story with {facts} distinct facts and no authentic photo: "
                                  "fact-card carousel", use_photos=[])
    t.append("C: not a carousel (%d photo(s), %d fact(s), simple_update=%s)" % (len(distinct), facts, profile.simple_update))
    if "reel" in avoid:
        encoder_ok = False

    # R before P only when narration clearly helps and visuals are authentic ------------------
    reel_wanted = narr >= config.REEL_NARRATION_MIN and encoder_ok
    reel_visuals = bool(photos) or illustrative_ok
    if reel_wanted and reel_visuals:
        t.append(f"R: narration value {narr:.2f} >= {config.REEL_NARRATION_MIN:.2f} and "
                 + ("authentic photos are available" if photos else "labelled illustrative footage is acceptable for this topic"))
        return finish("reel", "narration adds useful context (narration value %.2f)" % narr
                      + ("; visuals are real photographs" if photos else "; visuals are licensed illustrative footage, "
                                                                          "labelled as such"),
                      use_photos=photos[:4], voice=reel_voice(), illus=illustrative_ok)
    if reel_wanted:
        t.append("R: narration would help but there are no authentic visuals and stock footage would imply event footage")
    else:
        t.append(f"R: narration value {narr:.2f} below {config.REEL_NARRATION_MIN:.2f} (or encoder unavailable): no Reel")

    # P ---------------------------------------------------------------- single photo
    if photos and "photo" not in avoid:
        lead = photos[0]
        t.append(f"P: relevant photo available ({lead.role}, confidence {lead.confidence:.2f})")
        why = ("verified event photograph available" if lead.role == "event" else
               "relevant file photo available (labelled as such)")
        return finish("photo", f"{why}; original video unavailable; narration unnecessary", use_photos=photos[:1])
    t.append("P: no accepted photograph")

    # F ---------------------------------------------------------------- clean card
    return finish("card", "no authentic photograph or video could be matched to this story; publishing an original "
                          "fact card instead of a misleading picture or a generic Reel", use_photos=[])


# ----------------------------------------------------------------------------- decision record
def build_record(cluster: dict, profile, plan: MediaPlan | None, decision: Decision, *, trend_report=None,
                 collector_report: dict | None = None) -> dict:
    sig = cluster.get("signals", {})
    rec = {
        "story": {"id": profile.story_id, "title": cluster["lead"]["title"], "urls": [a["url"] for a in cluster["articles"][:6]],
                  "category": cluster["category"], "sources": cluster["sources"], "kind": cluster.get("kind"),
                  "why_ranked": cluster.get("why", "")},
        "trend_signals": {"score_breakdown": cluster.get("breakdown"), "signals": sig,
                          "providers": getattr(trend_report, "providers", None)},
        "corroboration": {"independent_sources": profile.independent_sources, "official_source": profile.official_source,
                          "conflicting_figures": profile.conflicting_figures},
        "analysis": profile.to_dict(),
        "collection": collector_report or {},
        "media": plan.summary() if plan else {},
        "decision": decision.to_dict(),
        "explanation": decision.explanation(),
        "outcome": {},
    }
    for a in rec["decision"]["photos"] + ([rec["decision"]["video"]] if rec["decision"]["video"] else []):
        a.pop("phash", None)
    return rec


def append_decision(data_dir: Path, record: dict) -> None:
    """data/decisions.jsonl: one JSON line per publishing attempt, newest last, capped at DECISION_LOG_MAX."""
    path = Path(data_dir) / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    lines.append(json.dumps(record, ensure_ascii=False, default=str))
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines[-config.DECISION_LOG_MAX:]) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_decisions(data_dir: Path, limit: int = 10) -> list[dict]:
    path = Path(data_dir) / "decisions.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def format_report(rec: dict) -> str:
    """Human-readable editorial note printed by `run --dry-run` and `explain`."""
    d, s, m = rec["decision"], rec["story"], rec.get("media", {})
    lines = [f"STORY     {s['title']}", f"SOURCES   {', '.join(s['sources'])}  ({s.get('kind')})",
             f"RANKING   {s.get('why_ranked', '')}", f"DECISION  {rec['explanation']}"]
    lines += ["  - " + x for x in d["trail"]]
    lines.append(f"MEDIA USE {d['media_use']}   voiceover={d['voiceover']}   illustrative_footage={d['illustrative_footage']}")
    for a in m.get("photos", []):
        lines.append(f"  accepted photo [{a['role']}/{a['permission']} conf {a['confidence']}] {a['title'][:60]} - {a['attribution']}")
    for a in m.get("videos", []):
        lines.append(f"  accepted video [{a['role']}/{a['permission']} conf {a['confidence']}] {a['title'][:60]}")
    for r in m.get("rejected", [])[:8]:
        lines.append(f"  rejected {r['kind']}: {r['title'][:50]} -> {r['reason']}")
    out = rec.get("outcome") or {}
    if out:
        lines.append("OUTCOME   " + json.dumps(out, ensure_ascii=False))
    return "\n".join(lines)


def credits_line(assets: list[MediaAsset], max_items: int = 3) -> str | None:
    """Caption credit for the photos used: licence-required wording first, then the source/provider names."""
    if not assets:
        return None
    seen, parts, vias = set(), [], []
    for a in assets:
        line = a.caption_credit or a.attribution or a.creator or a.source
        if line not in seen:
            seen.add(line)
            parts.append(line)
        via = {"commons": "Wikimedia Commons", "openverse": "Openverse", "official": "official source",
               "local": ""}.get(a.source, a.source)
        if via and via not in vias:
            vias.append(via)
    extra = f" and {len(parts) - max_items} more" if len(parts) > max_items else ""
    return ", ".join(parts[:max_items]) + extra + (" via " + ", ".join(vias) if vias else "")
