"""URL/title de-duplication, history filtering and cross-source story clustering."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

STOP = set("""a an the and or but if of to in on at for from by with as is are was were be been being it its this that
these those after before over under into than then also said says say new more most about up out off not no
will would could should may might can has have had do does did his her their our your you he she they we i
amid vs via per just now today latest report reports reported""".split())
_TRACK = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "ref", "ocid", "cmpid"}


def canonical_url(url: str) -> str:
    p = urlsplit(url.strip())
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if k.lower() not in _TRACK])
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower().removeprefix("www."), path, q, ""))


def tokens(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return frozenset(w for w in words if w not in STOP and len(w) > 2)


def title_key(title: str) -> str:
    return " ".join(sorted(tokens(title)))


def story_fingerprint(titles: list[str]) -> str:
    """Stable id for a story: built from the most common significant tokens of its titles."""
    counts: dict[str, int] = {}
    for t in titles:
        for w in tokens(t):
            counts[w] = counts.get(w, 0) + 1
    top = sorted(counts, key=lambda w: (-counts[w], w))[:6]
    return hashlib.sha1(" ".join(sorted(top)).encode()).hexdigest()[:14]


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedupe_batch(articles: list[dict]) -> list[dict]:
    """Drop the SAME article seen twice (same URL, or the same headline from the same outlet). The same headline from a
    DIFFERENT outlet is kept: that is corroboration (syndicated/wire copy), which clustering and ranking rely on."""
    seen_urls, seen_titles, out = set(), set(), []
    for a in articles:
        cu = canonical_url(a["url"])
        tk = (a.get("source", ""), title_key(a["title"]))
        if cu in seen_urls or tk in seen_titles:
            continue
        seen_urls.add(cu)
        seen_titles.add(tk)
        out.append(a)
    return out


def filter_published(articles: list[dict], history: list[dict]) -> list[dict]:
    urls = {canonical_url(h["url"]) for h in history if h.get("url")}
    tkeys = {title_key(h["title"]) for h in history if h.get("title")}
    return [a for a in articles if canonical_url(a["url"]) not in urls and title_key(a["title"]) not in tkeys]


def cluster_articles(articles: list[dict], threshold: float = 0.45) -> list[list[dict]]:
    """Union-find clustering of articles that cover the same story (title+summary token overlap)."""
    n = len(articles)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    toks = [tokens(a["title"]) for a in articles]
    for i in range(n):
        for j in range(i + 1, n):
            shared = toks[i] & toks[j]
            if len(shared) >= 3 and jaccard(toks[i], toks[j]) >= threshold:
                parent[find(i)] = find(j)
    groups: dict[int, list[dict]] = {}
    for i, a in enumerate(articles):
        groups.setdefault(find(i), []).append(a)
    return list(groups.values())


def similar_to_history(cluster: dict, history_titles: list[str], threshold: float = 0.5) -> bool:
    """True if any article of the cluster tells a story we already posted (even from another source)."""
    htoks = [tokens(t) for t in history_titles]
    for a in cluster["articles"]:
        at = tokens(a["title"])
        for ht in htoks:
            if len(at & ht) >= 3 and jaccard(at, ht) >= threshold:
                return True
    return False
