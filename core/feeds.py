"""RSS sources per category + source quality tiers (0..1)."""

FEEDS: dict[str, list[dict]] = {
    "News": [
        {"name": "NDTV News", "url": "https://feeds.feedburner.com/ndtvnews-top-stories"},
        {"name": "Times of India", "url": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"},
        {"name": "The Hindu", "url": "https://www.thehindu.com/news/national/feeder/default.rss"},
        {"name": "BBC News", "url": "http://feeds.bbci.co.uk/news/rss.xml"},
        {"name": "BBC World", "url": "http://feeds.bbci.co.uk/news/world/rss.xml"},
        {"name": "The Hindu International", "url": "https://www.thehindu.com/news/international/feeder/default.rss"},
        {"name": "Indian Express", "url": "https://indianexpress.com/section/india/feed/"},
        {"name": "Hindustan Times", "url": "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml"},
        {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
        {"name": "PIB (Government of India)", "url": "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"},
    ],
    "Business": [
        {"name": "Economic Times Markets", "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"},
        {"name": "The Hindu BusinessLine", "url": "https://www.thehindubusinessline.com/feeder/default.rss"},
        {"name": "BBC Business", "url": "http://feeds.bbci.co.uk/news/business/rss.xml"},
    ],
    "Science": [
        {"name": "NASA", "url": "https://www.nasa.gov/rss/dyn/breaking_news.rss"},
        {"name": "BBC Science", "url": "http://feeds.bbci.co.uk/news/science_and_environment/rss.xml"},
    ],
    "Technology": [
        {"name": "Economic Times Tech", "url": "https://economictimes.indiatimes.com/tech/rssfeeds/13357270.cms"},
        {"name": "TechCrunch", "url": "https://techcrunch.com/feed/"},
        {"name": "Wired", "url": "https://www.wired.com/feed/rss"},
        {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml"},
    ],
    "Sports": [
        {"name": "TOI Sports", "url": "https://timesofindia.indiatimes.com/rssfeeds/4719148.cms"},
        {"name": "NDTV Sports", "url": "https://feeds.feedburner.com/ndtvsports-latest"},
        {"name": "ESPN Cricinfo", "url": "https://www.espncricinfo.com/rss/content/story/feeds/0.xml"},
        {"name": "BBC Sport", "url": "http://feeds.bbci.co.uk/sport/rss.xml"},
    ],
    "Entertainment": [
        {"name": "Times of India Entertainment", "url": "https://timesofindia.indiatimes.com/rssfeeds/1081479906.cms"},
        {"name": "Variety", "url": "https://variety.com/feed/"},
        {"name": "Hollywood Reporter", "url": "https://www.hollywoodreporter.com/feed/"},
    ],
}

CATEGORIES = ("News", "Technology", "Sports", "Entertainment", "Business", "Science")


def parse_extra_feeds(raw: str) -> dict[str, list[dict]]:
    """EXTRA_FEEDS="News|The Print|https://theprint.in/feed/;Sports|Cricbuzz|https://..." (newlines also separate entries).
    Bad entries are skipped, never fatal."""
    out: dict[str, list[dict]] = {}
    for part in (raw or "").replace("\n", ";").split(";"):
        bits = [b.strip() for b in part.split("|")]
        if len(bits) != 3:
            continue
        cat = next((c for c in CATEGORIES if c.lower() == bits[0].lower()), None)
        if cat and bits[1] and bits[2].lower().startswith(("http://", "https://")):
            out.setdefault(cat, []).append({"name": bits[1], "url": bits[2]})
    return out


def load_sources_file(path: str | None) -> dict:
    """SOURCES_FILE (JSON): {"add": [{"category","name","url","quality"?}], "disable": ["Name"], "quality": {"Name": 0.9},
    "official_media_domains": ["pib.gov.in"]}. A missing or broken file is reported and ignored, never fatal."""
    import json
    import logging
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as e:
        logging.getLogger("feeds").warning("SOURCES_FILE %s ignored (%s)", path, type(e).__name__)
        return {}


def all_feeds(extra_raw: str | None = None) -> dict[str, list[dict]]:
    """Built-in feeds + EXTRA_FEEDS + SOURCES_FILE additions, minus DISABLED_SOURCES / SOURCES_FILE "disable",
    without duplicate URLs. Nothing about the source list is hardcoded into the collector."""
    from . import config
    spec = load_sources_file(config.SOURCES_FILE)
    disabled = {d.lower() for d in config.DISABLED_SOURCES} | {str(d).lower() for d in spec.get("disable", [])}
    for name, q in (spec.get("quality") or {}).items():
        if isinstance(q, (int, float)) and 0 <= q <= 1:
            SOURCE_QUALITY[name] = float(q)
    merged = {cat: [f for f in items if f["name"].lower() not in disabled] for cat, items in FEEDS.items()}
    for f in spec.get("add", []) or []:
        if isinstance(f, dict) and f.get("category") in CATEGORIES and str(f.get("url", "")).startswith("http") and f.get("name"):
            merged.setdefault(f["category"], []).append({"name": str(f["name"]), "url": f["url"]})
            if isinstance(f.get("quality"), (int, float)):
                SOURCE_QUALITY[str(f["name"])] = float(f["quality"])
    seen = {f["url"] for items in merged.values() for f in items}
    for cat, items in parse_extra_feeds(config.EXTRA_FEEDS if extra_raw is None else extra_raw).items():
        for f in items:
            if f["url"] not in seen and f["name"].lower() not in disabled:
                merged.setdefault(cat, []).append(f)
                seen.add(f["url"])
    return {cat: items for cat, items in merged.items() if items}


SOURCE_QUALITY: dict[str, float] = {
    "BBC News": 1.0, "BBC Sport": 1.0, "The Hindu": 0.95, "NDTV News": 0.9,
    "Times of India": 0.85, "TechCrunch": 0.9, "The Verge": 0.9, "Wired": 0.9,
    "Economic Times Tech": 0.85, "ESPN Cricinfo": 0.95, "NDTV Sports": 0.85,
    "TOI Sports": 0.8, "Variety": 0.9, "Hollywood Reporter": 0.9,
    "Times of India Entertainment": 0.75,
    "BBC World": 1.0, "The Hindu International": 0.95, "Indian Express": 0.9, "Hindustan Times": 0.85,
    "Al Jazeera": 0.9, "PIB (Government of India)": 1.0, "Economic Times Markets": 0.85,
    "The Hindu BusinessLine": 0.9, "BBC Business": 1.0, "NASA": 1.0, "BBC Science": 1.0,
}
DEFAULT_SOURCE_QUALITY = 0.7

# Category look & feel for the image cards: (gradient top, gradient bottom, accent)
CATEGORY_STYLE: dict[str, tuple[str, str, str]] = {
    "News": ("#0B1B3A", "#1D4ED8", "#FBBF24"),
    "Technology": ("#0F0A2E", "#6D28D9", "#22D3EE"),
    "Sports": ("#052E1B", "#047857", "#FDE047"),
    "Entertainment": ("#2A0A1F", "#BE185D", "#FDBA74"),
    "Business": ("#06210F", "#0F766E", "#FACC15"),
    "Science": ("#07142B", "#0E7490", "#A5F3FC"),
}
CATEGORY_EMOJI = {"News": "📰", "Technology": "💻", "Sports": "🏏", "Entertainment": "🎬", "Business": "📈", "Science": "🔬"}
CATEGORY_HASHTAGS = {
    "News": ["#news", "#india", "#currentaffairs", "#breakingnews", "#todayinnews"],
    "Technology": ["#tech", "#technology", "#ai", "#gadgets", "#innovation"],
    "Sports": ["#sports", "#cricket", "#sportsnews", "#football", "#athletes"],
    "Entertainment": ["#entertainment", "#bollywood", "#hollywood", "#movies", "#celebrity"],
    "Business": ["#business", "#economy", "#markets", "#finance", "#sensex"],
    "Science": ["#science", "#space", "#research", "#discovery", "#environment"],
}
