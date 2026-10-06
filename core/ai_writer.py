"""Instagram copywriting: Gemini (google-genai SDK) -> validated JSON -> caption + carousel slides.

Quality guards
- Output must be valid JSON that passes strict schema + length checks (auto-trimmed on word boundaries).
- "Why it matters" must be story-specific: generic filler is rejected, never published.
- Any multi-digit number in the output must exist in the source text (no invented statistics).
- If AI fails twice, a deterministic *extractive* writer builds the post from the sources. The fallback
  never prints placeholder text; sections it cannot fill honestly are simply omitted.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Callable

from . import config
from .feeds import CATEGORY_EMOJI, CATEGORY_HASHTAGS
from .health import mask_secrets
from .storage import prune_dict_by_age, read_json, write_json

log = logging.getLogger("ai_writer")

CAPTION_LIMIT = 2200
MAX_HASHTAGS = 12
GENERIC_PHRASES = (
    "key updates", "developing story", "stay tuned", "highlights key", "more details to follow",
    "read more", "click the link", "link in bio", "this story is developing", "important development",
    "makes headlines", "sparks conversation", "in today's fast-paced",
)
_NUM = re.compile(r"\d[\d,\.]*\d|\d")


class ContentError(ValueError):
    pass


_BAD_MODEL_WORDS = ("lite", "image", "tts", "live", "audio", "embedding", "exp", "thinking", "computer",
                    "robotics", "omni", "native", "vision", "learnlm", "gemma")


def pick_flash_models(names: list[str]) -> list[str]:
    """All general-purpose Flash models, best first (stable before preview, then newest)."""
    scored = []
    for raw in names:
        n = raw.split("/")[-1].lower()
        m = re.match(r"gemini-(\d+(?:\.\d+)?)-flash(.*)$", n)
        if not m or any(w in n for w in _BAD_MODEL_WORDS):
            continue
        scored.append((("preview" not in n, float(m.group(1))), n))
    seen, out = set(), []
    for _, n in sorted(scored, reverse=True):
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def pick_flash_model(names: list[str]) -> str | None:
    best = pick_flash_models(names)
    return best[0] if best else None


# ----------------------------------------------------------------- text utils
def trim(text: str, limit: int, keep_breaks: bool = False) -> str:
    """Trim to <= limit chars on a sentence/word boundary (adds an ellipsis only when cut mid-sentence).
    keep_breaks=True preserves paragraph breaks (used for the Instagram caption body)."""
    text = text or ""
    if keep_breaks:
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r" ?\n ?", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    else:
        text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if m >= limit * 0.55:
        return cut[: m + 1].strip()
    cut = cut[: max(cut.rfind(" "), 1)].rstrip(" ,;:-–")
    return cut + "…"


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUM.findall(text)}


def numbers_grounded(generated: str, source: str) -> list[str]:
    src = _numbers(source)
    return [n for n in _numbers(generated) if len(n.replace(".", "")) >= 2 and n not in src]


def is_generic(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in GENERIC_PHRASES)


# ----------------------------------------------------------------- hashtags
def build_hashtags(ai_tags: list[str] | None, category: str) -> list[str]:
    out: list[str] = []

    def add(tag: str) -> None:
        t = re.sub(r"[^A-Za-z0-9_]", "", tag.lstrip("#")).lower()
        if 3 <= len(t) <= 30 and f"#{t}" not in out:
            out.append(f"#{t}")

    for t in config.BRAND_HASHTAGS:
        add(t)
    for t in (ai_tags or []):
        if isinstance(t, str):
            add(t)
    for t in CATEGORY_HASHTAGS.get(category, []):
        if len(out) >= 10:
            break
        add(t)
    return out[:MAX_HASHTAGS]


# ----------------------------------------------------------------- caption
def compose_caption(content: dict, cluster: dict, footage_credit: str | None = None) -> str:
    emoji = CATEGORY_EMOJI.get(cluster["category"], "📰")
    credit = ", ".join(cluster["sources"][:3]) + (f" | Footage: {footage_credit}" if footage_credit else "")
    tail_parts = []
    if content.get("why_it_matters"):
        tail_parts.append(f"💡 Why it matters: {content['why_it_matters']}")
    tail_parts.append(f"📌 Source: {credit}")
    tail_parts.append(f"👉 Follow {config.ACCOUNT_HANDLE} for daily updates. Save & share!")
    tail = "\n\n".join(tail_parts)
    tags = " ".join(content["hashtags"])
    head = f"{emoji} {content['hook']}"
    fixed = len(head) + len(tail) + len(tags) + 6
    body = trim(content["caption"], max(120, CAPTION_LIMIT - fixed - 10), keep_breaks=True)
    caption = f"{head}\n\n{body}\n\n{tail}\n\n{tags}"
    assert len(caption) <= CAPTION_LIMIT, len(caption)
    return caption


# ----------------------------------------------------------------- validation
def validate_content(data: dict, source_text: str, category: str) -> dict:
    if not isinstance(data, dict):
        raise ContentError("response is not a JSON object")
    hook = trim(str(data.get("hook", "")), 80)
    if len(hook) < 15:
        raise ContentError("hook too short")
    slides_in = data.get("slides")
    if not isinstance(slides_in, list) or not (2 <= len(slides_in) <= 5):
        raise ContentError("slides must be a list of 2-5 items")
    slides = []
    for s in slides_in:
        if not isinstance(s, dict):
            raise ContentError("slide is not an object")
        heading, body = trim(str(s.get("heading", "")), 42), trim(str(s.get("body", "")), 210)
        if len(heading) < 3 or len(body) < 30:
            raise ContentError("slide heading/body too short")
        slides.append({"heading": heading, "body": body})
    caption = trim(str(data.get("caption", "")), 900, keep_breaks=True)
    if len(caption) < 80:
        raise ContentError("caption too short")
    why = trim(str(data.get("why_it_matters", "")), 220)
    if len(why) < 40:
        raise ContentError("why_it_matters too short")
    for label, txt in (("hook", hook), ("why_it_matters", why), ("caption", caption),
                       *[(f"slide{i}", s["body"]) for i, s in enumerate(slides)]):
        if is_generic(txt):
            raise ContentError(f"{label} contains generic filler")
    blob = " ".join([hook, why, caption] + [s["heading"] + " " + s["body"] for s in slides])
    bad = numbers_grounded(blob, source_text)
    if bad:
        raise ContentError(f"numbers not found in sources: {bad[:3]}")
    return {
        "hook": hook,
        "slides": slides,
        "why_it_matters": why,
        "caption": caption,
        "hashtags": build_hashtags(data.get("hashtags"), category),
    }


# ----------------------------------------------------------------- reel script
def validate_reel(data: dict, source_text: str, category: str) -> dict:
    """Narration for a voiced Reel: short spoken sentences, grounded numbers, no filler."""
    if not isinstance(data, dict):
        raise ContentError("response is not a JSON object")
    hook = trim(str(data.get("hook", "")), 70)
    if len(hook) < 15:
        raise ContentError("hook too short")
    narr_in = data.get("narration")
    if isinstance(narr_in, str):
        narr_in = re.split(r"(?<=[.!?])\s+", narr_in.strip())
    if not isinstance(narr_in, list) or not (3 <= len(narr_in) <= 8):
        raise ContentError("narration must be 3-8 sentences")
    narration = []
    for sent in narr_in:
        sent = re.sub(r"\s+", " ", str(sent)).strip()
        words = sent.split()
        if not (3 <= len(words) <= 26):
            raise ContentError("each narration sentence must be 3-26 words")
        if re.search(r"https?://|#\w|@\w", sent):
            raise ContentError("narration must not contain links, hashtags or handles")
        narration.append(sent)
    total_words = sum(len(x.split()) for x in narration)
    if not (30 <= total_words <= 85):
        raise ContentError(f"narration must be 30-85 words (got {total_words})")
    caption = trim(str(data.get("caption", "")), 900, keep_breaks=True)
    if len(caption) < 80:
        raise ContentError("caption too short")
    why = trim(str(data.get("why_it_matters", "")), 220)
    if len(why) < 40:
        raise ContentError("why_it_matters too short")
    for label, txt in (("hook", hook), ("why_it_matters", why), ("caption", caption),
                       *[(f"narration{i}", n) for i, n in enumerate(narration)]):
        if is_generic(txt):
            raise ContentError(f"{label} contains generic filler")
    bad = numbers_grounded(" ".join([hook, why, caption] + narration), source_text)
    if bad:
        raise ContentError(f"numbers not found in sources: {bad[:3]}")
    queries = []
    for q in (data.get("broll_queries") or []):
        if isinstance(q, str):
            q = " ".join(re.sub(r"[^A-Za-z ]", " ", q).lower().split()[:5])
            if q and q not in queries:
                queries.append(q)
    return {"hook": hook, "narration": narration, "broll_queries": queries[:5], "why_it_matters": why,
            "caption": caption, "hashtags": build_hashtags(data.get("hashtags"), category)}


def fallback_reel(cluster: dict) -> dict:
    """Extractive narration when AI is unavailable: headline + up to two factual sentences from the sources."""
    base = fallback_content(cluster)
    lead = cluster["lead"]
    sentences = [lead["title"].rstrip(".") + "."]
    for sl in base["slides"]:
        if sl["body"].strip() and sl["body"].strip().lower() != lead["title"].strip().lower():
            sentences.append(sl["body"].strip())
    narration = [trim(x, 170) for x in sentences[:3]]
    return {"hook": base["hook"], "narration": narration, "broll_queries": [], "why_it_matters": None,
            "caption": base["caption"], "hashtags": base["hashtags"]}


# ----------------------------------------------------------------- fallback
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9“\"'])")


def fallback_content(cluster: dict) -> dict:
    lead = cluster["lead"]
    sentences: list[str] = []
    seen: set[str] = set()
    for a in cluster["articles"][:3]:
        for s in _SENT.split(a["summary"]):
            s = s.strip()
            key = s.lower()[:50]
            if 40 <= len(s) <= 260 and key not in seen and key not in lead["title"].lower():
                seen.add(key)
                sentences.append(s)
    heads = ["What happened", "The details", "The context"]
    slides = [{"heading": heads[i], "body": trim(s, 210)} for i, s in enumerate(sentences[:3])]
    if not slides:
        slides = [{"heading": "What happened", "body": trim(lead["title"], 210)}]
    caption = " ".join(sentences[:2]) if sentences else lead["title"]
    if len(caption) < 20:
        caption = lead["title"]
    return {
        "hook": trim(lead["title"], 80),
        "slides": slides,
        "why_it_matters": None,  # honest: omitted rather than filled with boilerplate
        "caption": trim(caption, 900),
        "hashtags": build_hashtags(None, cluster["category"]),
    }


# ----------------------------------------------------------------- writer
def build_prompt(cluster: dict) -> tuple[str, str]:
    parts = []
    for a in cluster["articles"][:3]:
        parts.append(f"[{a['source']}] {a['title']}\n{a['summary'][:700]}")
    source_text = "\n\n".join(parts)
    prompt = f"""You are the editor of a professional Instagram news page ({config.BRAND_NAME}).
Turn the SOURCE MATERIAL into a 5-slide carousel post for the category "{cluster['category']}".

STRICT RULES
- Use ONLY facts present in the SOURCE MATERIAL. Never invent numbers, quotes, names, dates or outcomes.
- Neutral, factual, clear English. No clickbait, no all-caps shouting, no emojis inside slides.
- "why_it_matters" must be specific to THIS story (impact on people, money, a team, a user, a policy).
  Never write filler such as "developing story" or "key updates".
- Do not mention links, "link in bio" or "stay tuned".
- "slides" must contain exactly 3 items (the cover and closing slides are added automatically).

Return ONLY JSON with exactly this shape:
{{
  "hook": "cover headline, 20-70 chars, accurate and punchy",
  "slides": [{{"heading": "2-5 words", "body": "1-2 short sentences, max 190 chars"}}],
  "why_it_matters": "1-2 sentences, 60-200 chars, specific impact",
  "caption": "2 short paragraphs, 200-700 chars, no hashtags",
  "hashtags": ["8 relevant lowercase hashtags without spaces"]
}}

SOURCE MATERIAL
{source_text}
"""
    return prompt, source_text


def build_reel_prompt(cluster: dict) -> tuple[str, str]:
    parts = [f"[{a['source']}] {a['title']}\n{a['summary'][:700]}" for a in cluster["articles"][:3]]
    source_text = "\n\n".join(parts)
    prompt = f"""You write VOICEOVER SCRIPTS for fast, engaging Instagram Reels about news ({config.BRAND_NAME}).
Category: "{cluster['category']}". Turn the SOURCE MATERIAL into a 20-25 second spoken script.

STRICT RULES
- Use ONLY facts in the SOURCE MATERIAL. Never invent numbers, quotes, names, dates or outcomes.
- Write for the EAR: short, conversational, punchy sentences (max 18 words), active voice, no jargon.
- Sentence 1 must be a strong hook that makes people keep watching (a surprising fact or question from the source).
- 4-6 sentences, 45-70 words in total. Do NOT add a "follow us" line (it is added automatically).
- No emojis, hashtags, links, handles, or filler like "developing story" or "stay tuned".
- "why_it_matters" must be specific to THIS story.
- "broll_queries": 4-5 stock-footage search terms, MOST SPECIFIC FIRST, each 2-3 plain words naming something a
  camera can actually film (objects, places, actions). NO abstract words (issues, appeals, pending, policy, crisis,
  trend), NO people names, brands or place names. Examples: election story -> "voting ballot box", "people voting
  queue"; court story -> "courtroom gavel", "legal documents"; cricket win -> "cricket team celebration",
  "stadium crowd cheering"; chip launch -> "circuit board", "smartphone screen"; flood -> "flood water street".

Return ONLY JSON with exactly this shape:
{{
  "hook": "on-screen headline, 20-65 chars",
  "narration": ["sentence one", "sentence two"],
  "broll_queries": ["term one", "term two", "term three"],
  "why_it_matters": "1-2 sentences, 60-200 chars",
  "caption": "2 short paragraphs, 200-700 chars, no hashtags",
  "hashtags": ["8 relevant lowercase hashtags without spaces"]
}}

SOURCE MATERIAL
{source_text}
"""
    return prompt, source_text


class AIWriter:
    def __init__(self, generate_json: Callable[[str], str] | None = None, *,
                 cache_path: Path | None = None, api_key: str | None = None, model: str | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.api_key = api_key if api_key is not None else config.GEMINI_API_KEY
        self.model = model or config.GEMINI_MODEL
        self._generate = generate_json or (self._gemini_generate if self.api_key else None)
        self.cache_path = cache_path or (config.DATA_DIR / "ai_cache.json")
        self._sleep = sleep
        self._client = None
        self._alts_loaded = False

    @property
    def ai_enabled(self) -> bool:
        return self._generate is not None

    def _gemini_generate(self, prompt: str) -> str:
        """Call Gemini with resilience: retry 5xx/429 with backoff, and switch to another Flash model
        when the configured one is retired (404) or stays overloaded."""
        from google import genai
        from google.genai import errors, types
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        cfg = types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0.4, max_output_tokens=4096,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        queue, tried, last = [self.model], [], None
        while queue:
            model = queue.pop(0)
            tried.append(model)
            for attempt in range(3):
                try:
                    resp = self._client.models.generate_content(model=model, contents=prompt, config=cfg)
                except errors.ServerError as e:  # 5xx: overloaded / unavailable -> transient
                    last = e
                    self._sleep(2.0 * (attempt + 1))
                except errors.ClientError as e:
                    if getattr(e, "code", None) == 429:
                        last = e
                        self._sleep(3.0 * (attempt + 1))
                    elif getattr(e, "code", None) == 404:
                        last = e
                        break  # model retired/unknown -> try another one
                    else:
                        raise
                else:
                    if model != self.model:
                        log.warning("Gemini model '%s' unavailable -> using '%s'. Set GEMINI_MODEL=%s in .env",
                                    self.model, model, model)
                        self.model = model
                    return resp.text or ""
            if not queue and not self._alts_loaded:
                self._alts_loaded = True  # discover alternatives once per process
                try:
                    names = [m.name for m in self._client.models.list()
                             if "generateContent" in (getattr(m, "supported_actions", None) or ["generateContent"])]
                    queue = [n for n in pick_flash_models(names) if n not in tried][:3]
                except Exception:  # noqa: BLE001
                    queue = []
        assert last is not None
        raise last

    @staticmethod
    def _parse(raw: str) -> dict:
        raw = raw.strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, flags=re.S)
            if not m:
                raise ContentError("no JSON object in response")
            return json.loads(m.group(0))

    def write(self, cluster: dict) -> tuple[dict, str]:
        """Carousel copy. Returns (content, mode) where mode in {'ai','cache','fallback'}."""
        return self._write(cluster, "carousel", build_prompt, validate_content, fallback_content)

    def write_reel(self, cluster: dict) -> tuple[dict, str]:
        """Voiceover script + caption for a Reel."""
        return self._write(cluster, "reel", build_reel_prompt, validate_reel, fallback_reel)

    def _write(self, cluster: dict, kind: str, build, validate, fallback) -> tuple[dict, str]:
        key = f"{kind}:{cluster['fingerprint']}:{cluster['lead']['id']}"
        cache = read_json(self.cache_path, {})
        hit = cache.get(key)
        if hit and time.time() - hit["ts"] < config.AI_CACHE_TTL_HOURS * 3600:
            return hit["content"], "cache"

        if self.ai_enabled:
            prompt, source_text = build(cluster)
            last_err = ""
            for attempt in range(1, config.AI_MAX_ATTEMPTS + 1):
                try:
                    p = prompt if not last_err else prompt + f"\nYour previous answer was rejected: {last_err}. Fix it."
                    content = validate(self._parse(self._generate(p)), source_text, cluster["category"])
                    cache[key] = {"ts": time.time(), "content": content}
                    cache = prune_dict_by_age(cache, max_entries=config.AI_CACHE_MAX_ENTRIES,
                                              max_age_hours=config.AI_CACHE_TTL_HOURS)
                    write_json(self.cache_path, cache)
                    return content, "ai"
                except Exception as e:
                    last_err = str(e)[:160]
                    log.warning("AI attempt %d/%d failed: %s: %s", attempt, config.AI_MAX_ATTEMPTS, type(e).__name__,
                                mask_secrets(str(e))[:220])
                    if attempt < config.AI_MAX_ATTEMPTS:
                        self._sleep(2.0 * attempt)
        return fallback(cluster), "fallback"
