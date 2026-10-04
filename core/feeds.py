"""RSS sources per category + source quality tiers (0..1)."""

FEEDS: dict[str, list[dict]] = {
    "News": [
        {"name": "NDTV News", "url": "https://feeds.feedburner.com/ndtvnews-top-stories"},
        {"name": "Times of India", "url": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"},
        {"name": "The Hindu", "url": "https://www.thehindu.com/news/national/feeder/default.rss"},
        {"name": "BBC News", "url": "http://feeds.bbci.co.uk/news/rss.xml"},
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

SOURCE_QUALITY: dict[str, float] = {
    "BBC News": 1.0, "BBC Sport": 1.0, "The Hindu": 0.95, "NDTV News": 0.9,
    "Times of India": 0.85, "TechCrunch": 0.9, "The Verge": 0.9, "Wired": 0.9,
    "Economic Times Tech": 0.85, "ESPN Cricinfo": 0.95, "NDTV Sports": 0.85,
    "TOI Sports": 0.8, "Variety": 0.9, "Hollywood Reporter": 0.9,
    "Times of India Entertainment": 0.75,
}
DEFAULT_SOURCE_QUALITY = 0.7

# Category look & feel for the image cards: (gradient top, gradient bottom, accent)
CATEGORY_STYLE: dict[str, tuple[str, str, str]] = {
    "News": ("#0B1B3A", "#1D4ED8", "#FBBF24"),
    "Technology": ("#0F0A2E", "#6D28D9", "#22D3EE"),
    "Sports": ("#052E1B", "#047857", "#FDE047"),
    "Entertainment": ("#2A0A1F", "#BE185D", "#FDBA74"),
}
CATEGORY_EMOJI = {"News": "📰", "Technology": "💻", "Sports": "🏏", "Entertainment": "🎬"}
CATEGORY_HASHTAGS = {
    "News": ["#news", "#india", "#currentaffairs", "#breakingnews", "#todayinnews"],
    "Technology": ["#tech", "#technology", "#ai", "#gadgets", "#innovation"],
    "Sports": ["#sports", "#cricket", "#sportsnews", "#football", "#athletes"],
    "Entertainment": ["#entertainment", "#bollywood", "#hollywood", "#movies", "#celebrity"],
}
