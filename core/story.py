"""Story analysis for editorial decisions: what kind of story is this, how well is it corroborated, how much would
narration help, and is it safe to publish yet?

Everything here is derived from the collected articles (titles, summaries, timestamps, publishers). Nothing is
invented: a missing signal stays missing (None / False) instead of being estimated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import config
from .commons import GEO_WORDS, INSTITUTION_WORDS, _HEADLINE_VERBS, _LEAD_STOP, entity_queries
from .dedupe import STOP
from .feeds import DEFAULT_SOURCE_QUALITY, SOURCE_QUALITY
from .provenance import domain_of

TOPIC_WORDS: dict[str, set[str]] = {
    "election": {"election", "elections", "poll", "polls", "voting", "voters", "ballot", "candidate", "bypoll", "bypolls",
                 "mandate", "exit", "constituency", "counting", "lok", "sabha", "assembly"},
    "politics": {"minister", "government", "parliament", "bjp", "congress", "party", "opposition", "chief", "cabinet",
                 "prime", "president", "mp", "mla", "policy", "bill", "scheme", "supreme", "court", "verdict"},
    "business": {"market", "markets", "sensex", "nifty", "shares", "stock", "stocks", "economy", "gdp", "inflation",
                 "rbi", "bank", "profit", "revenue", "ipo", "merger", "acquisition", "tariff", "budget", "rupee"},
    "technology": {"ai", "chip", "software", "app", "startup", "smartphone", "iphone", "android", "openai", "robot",
                   "cybersecurity", "semiconductor", "laptop", "gpu", "launch", "launches", "unveils"},
    "sports": {"cricket", "match", "wicket", "innings", "tournament", "league", "ipl", "fifa", "football", "tennis",
               "olympic", "olympics", "final", "semi-final", "trophy", "medal", "coach", "squad", "world", "cup"},
    "entertainment": {"film", "movie", "actor", "actress", "trailer", "bollywood", "hollywood", "oscar", "album",
                      "singer", "song", "series", "netflix", "box", "office", "director"},
    "science": {"nasa", "isro", "space", "satellite", "rocket", "mission", "moon", "mars", "researchers", "study",
                "scientists", "discovery", "climate", "telescope", "vaccine"},
    "disaster": {"earthquake", "tsunami", "flood", "floods", "cyclone", "landslide", "wildfire", "storm", "hurricane",
                 "disaster", "collapse", "evacuated"},
    "conflict": {"war", "attack", "missile", "airstrike", "ceasefire", "troops", "militants", "terror", "border",
                 "shelling", "hostages", "strike"},
    "health": {"outbreak", "virus", "disease", "hospital", "vaccine", "health", "cases", "epidemic", "pandemic"},
    "crime": {"arrested", "arrest", "police", "murder", "fraud", "scam", "accused", "charged", "investigation"},
}
SENSITIVE_TOPICS = ("election", "disaster", "conflict", "health", "crime")
# Topics where the picture must show the real event (never "just something similar")
EVENT_TOPICS = ("election", "disaster", "conflict", "sports", "crime", "health")

ALLEGATION_RE = re.compile(r"\b(alleg\w*|accus\w*|claims?|claimed|reportedly|unconfirmed|rumou?rs?|denies|denied|"
                           r"sources? said|suspect\w*|charged with|blamed)\b", re.I)
# Titles that announce one concrete fact: a result, an appointment, a date, a statement
SIMPLE_RE = re.compile(r"\b(appointed|appoints|named|resigns?|resigned|wins?|won|beats?|defeats?|defeated|elected|"
                       r"dies|dead|passes away|announces?|announced|declared|sworn|retires?|signs?|bags?|clinch\w*|"
                       r"tops?|leads?|results?|scores?|hits?|ban|bans|banned|approves?|approved|launch(?:es|ed)?|"
                       r"unveils?|unveiled|releases?|released|sets? date)\b", re.I)
EXPLAIN_RE = re.compile(r"\b(explained|explainer|what (?:it|this) means|how (?:it|this|to)|why|impact|implications?|"
                        r"policy|scheme|bill|budget|reform|study|researchers?|scientists?|new technology|"
                        r"timeline|background|analysis|breakdown|rules?|guidelines?|framework)\b", re.I)
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9“\"'])")
_FIG_RE = re.compile(r"(\d[\d,]*)\s+(?:people\s+|persons\s+)?(?:were\s+|are\s+)?(killed|dead|deaths?|died|injured|missing|seats|votes|runs|wickets)", re.I)
_YEAR_RE = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")


@dataclass
class StoryProfile:
    story_id: str
    title: str
    text: str
    category: str
    entities: list[str] = field(default_factory=list)
    single_entities: list[str] = field(default_factory=list)
    years: set[int] = field(default_factory=set)
    event_year: int = 0
    topics: set[str] = field(default_factory=set)
    sensitive: list[str] = field(default_factory=list)
    allegation: bool = False
    independent_sources: int = 1
    official_source: bool = False
    conflicting_figures: list[str] = field(default_factory=list)
    corroboration: float = 0.0
    confidence: float = 0.0
    narration_value: float = 0.0
    simple_update: bool = False
    fact_points: int = 0
    event_story: bool = False
    hold_reason: str = ""

    def to_dict(self) -> dict:
        return {"story_id": self.story_id, "category": self.category, "entities": self.entities[:6],
                "single_entities": self.single_entities,
                "topics": sorted(self.topics), "sensitive_topics": self.sensitive, "allegation_language": self.allegation,
                "independent_sources": self.independent_sources, "official_source": self.official_source,
                "conflicting_figures": self.conflicting_figures, "corroboration": round(self.corroboration, 2),
                "confidence": round(self.confidence, 2), "narration_value": round(self.narration_value, 2),
                "simple_update": self.simple_update, "fact_points": self.fact_points, "event_story": self.event_story,
                "hold_reason": self.hold_reason}


_NOT_ENTITIES = {"government", "minister", "ministry", "police", "court", "officials", "official", "company", "report",
                 "study", "team", "state", "centre", "center", "party", "leader", "leaders", "people", "city", "india's",
                 "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
                 "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                 "explained", "exclusive", "breaking", "watch", "live", "opinion", "analysis", "new", "first", "latest",
                 "major", "big", "top", "full", "here", "this", "what", "why", "how", "who", "will", "has", "have"}


def single_entities(title: str) -> list[str]:
    """Single proper nouns in a headline: 'India', 'England', 'Tesla', 'BJP'. They are weaker evidence than multi-word
    names (a country can appear in any photo), so the media matcher needs two of them, or one non-geographic one."""
    toks = re.findall(r"[A-Za-z][A-Za-z'’-]*", title or "")
    if not toks:
        return []
    caps = [t for t in toks if t[0].isupper()]
    if len(toks) >= 5 and len(caps) / len(toks) > 0.7:      # Title Case Headline: capital letters prove nothing
        return []
    out: list[str] = []
    for t in toks:
        low = t.lower().strip("'’-")
        if (t[0].isupper() and len(low) >= 2 and low not in STOP and low not in _HEADLINE_VERBS and low not in _LEAD_STOP
                and low not in _NOT_ENTITIES and low not in {x.lower() for x in out}):
            out.append(t.strip("'’-"))
    return out[:5]


def _topics(text: str) -> set[str]:
    words = set(re.findall(r"[a-z][a-z\-]+", text.lower()))
    out = set()
    for name, kws in TOPIC_WORDS.items():
        # two hits for broad topics; one is enough for unambiguous event words
        need = 1 if name in {"disaster", "election", "conflict"} else 2
        if len(words & kws) >= need:
            out.add(name)
    return out


def _figures(text: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for num, kind in _FIG_RE.findall(text):
        k = {"deaths": "dead", "death": "dead", "died": "dead", "killed": "dead"}.get(kind.lower(), kind.lower())
        out.setdefault(k, set()).add(num.replace(",", ""))
    return out


def is_official_source(article: dict) -> bool:
    d = domain_of(article.get("url", ""))
    return (d.endswith(".gov.in") or d.endswith(".gov") or d.endswith(".nic.in") or d.endswith("nasa.gov")
            or str(article.get("source", "")).lower().startswith(("pib", "nasa")))


def analyse(cluster: dict, now: datetime | None = None) -> StoryProfile:
    now = now or datetime.now(timezone.utc)
    arts = cluster["articles"]
    lead = cluster["lead"]
    text = " ".join(a["title"] + ". " + a["summary"] for a in arts[:4])
    prof = StoryProfile(story_id=str(cluster.get("fingerprint", "")), title=lead["title"], text=text,
                        category=cluster["category"])
    from .media_match import strip_roles          # local import: media_match imports this package's helpers
    ents: list[str] = []
    for e in entity_queries(lead["title"] + ". " + lead.get("summary", "")[:600], limit=6):
        e = strip_roles(e)
        if e.lower() not in {x.lower() for x in ents}:
            ents.append(e)
    prof.entities = ents
    prof.single_entities = single_entities(lead["title"])
    prof.years = {int(y) for y in _YEAR_RE.findall(text)}
    pub = [datetime.fromisoformat(a["published"]) for a in arts if a.get("published")]
    prof.event_year = max(prof.years) if prof.years else (max(pub).year if pub else now.year)
    prof.topics = _topics(text)
    prof.sensitive = [t for t in SENSITIVE_TOPICS if t in prof.topics]
    prof.allegation = bool(ALLEGATION_RE.search(text))
    prof.independent_sources = len(set(cluster.get("sources") or [a["source"] for a in arts]))
    prof.official_source = any(is_official_source(a) for a in arts)
    prof.event_story = bool(set(prof.topics) & set(EVENT_TOPICS)) or bool(cluster.get("breaking"))

    # conflicting figures between outlets (e.g. different death tolls): qualify, or hold when it is an election result
    per_article = [_figures(a["title"] + " " + a["summary"]) for a in arts[:6]]
    for kind in {k for d in per_article for k in d}:
        vals = {v for d in per_article for v in d.get(kind, set())}
        if len(vals) > 1:
            prof.conflicting_figures.append(f"{kind}: {', '.join(sorted(vals, key=int)[:4])}")

    quality = max((SOURCE_QUALITY.get(s, DEFAULT_SOURCE_QUALITY) for s in cluster.get("sources", [])), default=0.5)
    prof.corroboration = min(1.0, (prof.independent_sources - 1) / 3.0) + (0.0 if not prof.official_source else 0.0)
    fresh = 1.0 if cluster.get("age_hours", 99) <= 6 else 0.5 if cluster.get("age_hours", 99) <= 24 else 0.2
    summary_len = min(1.0, len(lead.get("summary", "")) / 240.0)
    prof.confidence = min(1.0, 0.30 * quality + 0.35 * prof.corroboration + 0.10 * fresh + 0.10 * summary_len
                          + (0.15 if prof.official_source else 0.0))

    sentences = []
    seen = set()
    for a in arts[:4]:
        for s in _SENT.split(a["summary"]):
            s = s.strip()
            k = s.lower()[:50]
            if 40 <= len(s) <= 260 and k not in seen and k not in lead["title"].lower():
                seen.add(k)
                sentences.append(s)
    prof.fact_points = len(sentences)

    nv = 0.15
    nv += min(0.36, 0.12 * len(EXPLAIN_RE.findall(text)))
    nv += 0.2 if len(" ".join(a["summary"] for a in arts[:3])) > 450 else 0.0
    nv += 0.1 if prof.fact_points >= 4 else 0.0
    nv += 0.1 if prof.independent_sources >= 4 else 0.0
    simple = bool(SIMPLE_RE.search(lead["title"]))
    nv -= 0.3 if simple else 0.0
    prof.narration_value = max(0.0, min(1.0, nv))
    prof.simple_update = simple and prof.narration_value < 0.6
    prof.hold_reason = hold_reason(prof)
    return prof


def hold_reason(p: StoryProfile) -> str:
    """Why this story must wait (empty string = fine to publish). Accuracy beats timeliness."""
    need = config.MIN_CORROBORATION_SENSITIVE
    if p.sensitive and p.independent_sources < need and not p.official_source:
        return (f"held: {'/'.join(p.sensitive)} story reported by only {p.independent_sources} outlet(s); "
                f"needs {need} independent sources or an official source")
    if p.conflicting_figures and "election" in p.sensitive:
        return "held: outlets report different figures for an election story (" + "; ".join(p.conflicting_figures) + ")"
    if p.confidence < config.EDITORIAL_MIN_CONFIDENCE:
        return f"held: story confidence {p.confidence:.2f} below {config.EDITORIAL_MIN_CONFIDENCE:.2f}"
    return ""


def writer_guidance(p: StoryProfile) -> str:
    """Extra instructions for the copywriter, derived from the story analysis."""
    lines = []
    if p.allegation:
        lines.append("The sources contain allegations/claims: attribute each one to who said it ('police allege', "
                     "'the party claims'); never state an allegation as established fact.")
    if p.conflicting_figures:
        lines.append("Outlets give different figures (" + "; ".join(p.conflicting_figures) + "): say that reports vary "
                     "and attribute the numbers; do not pick one silently.")
    if p.sensitive:
        lines.append("Sensitive topic (" + ", ".join(p.sensitive) + "): stay strictly factual, no speculation, no blame.")
    return "\n".join(lines)


def institution_names(entities: list[str]) -> list[str]:
    return [e for e in entities if {w.lower() for w in e.split()} & INSTITUTION_WORDS]
