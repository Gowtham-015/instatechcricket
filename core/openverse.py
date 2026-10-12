"""Openverse (WordPress Foundation's open-licence search engine): CC-licensed photos from Flickr, NASA,
Smithsonian ... with licence + creator metadata. The legitimate replacement for "Google Images with usage rights"
(Google's image search API is closed to new users, and its usage-rights filter is not reliable).

Only commercial-use licences WITHOUT share-alike are requested (CC BY, CC0, Public Domain Mark) and re-checked on
every result. Anonymous access is limited (about 5 requests/hour, 100/day) so results are cached for 24 h and a free
OAuth client (OPENVERSE_CLIENT_ID / OPENVERSE_CLIENT_SECRET) is recommended. A 429 disables the source for the run.
"""
from __future__ import annotations

import logging
import math
import re
import time
from pathlib import Path

import requests

from . import config
from .commons import REJECT_TITLE_WORDS, PhotoSource, context_ok
from .stock import overlap, query_terms
from .storage import prune_dict_by_age, read_json, write_json

log = logging.getLogger("openverse")
API = "https://api.openverse.org/v1"
CACHE_HOURS = 24
ALLOWED = {"by", "cc0", "pdm"}


class OpenverseClient(PhotoSource):
    name = "openverse"

    def __init__(self, session=None, cache_path: Path | None = None, client_id: str | None = None,
                 client_secret: str | None = None):
        self.http = session or requests
        self.cache_path = cache_path or (config.DATA_DIR / "openverse_cache.json")
        self.client_id = client_id if client_id is not None else config.OPENVERSE_CLIENT_ID
        self.client_secret = client_secret if client_secret is not None else config.OPENVERSE_CLIENT_SECRET
        self.enabled = True
        self._token: tuple[str, float] | None = None

    def _auth(self) -> dict:
        h = dict(self.headers)
        if self.client_id and self.client_secret:
            if not self._token or self._token[1] < time.time() + 60:
                r = self.http.post(f"{API}/auth_tokens/token/", timeout=20, data={
                    "grant_type": "client_credentials", "client_id": self.client_id,
                    "client_secret": self.client_secret})
                if r.status_code == 200:
                    j = r.json()
                    self._token = (j["access_token"], time.time() + int(j.get("expires_in", 3600)))
            if self._token:
                h["Authorization"] = f"Bearer {self._token[0]}"
        return h

    def search(self, query: str) -> list[dict]:
        cache = read_json(self.cache_path, {})
        hit = cache.get(f"q:{query}")
        if hit and time.time() - hit["ts"] < CACHE_HOURS * 3600:
            return hit["results"]
        r = self.http.get(f"{API}/images/", headers=self._auth(), timeout=25, params={
            "q": query, "license": "by,cc0,pdm", "license_type": "commercial", "category": "photograph",
            "source": config.OPENVERSE_SOURCES, "filetype": "jpg,png", "mature": "false", "page_size": 20})
        if r.status_code == 429:
            self.enabled = False  # rate limited: stop asking for the rest of this run
            raise RuntimeError("Openverse rate limit reached (register a free client: see README)")
        if r.status_code != 200:
            raise RuntimeError(f"Openverse HTTP {r.status_code}")
        keep = ("id", "title", "url", "creator", "license", "license_version", "tags", "source", "width", "height",
                "mature", "foreign_landing_url", "detail_url")
        results = [{k: x.get(k) for k in keep} for x in r.json().get("results", [])]
        cache[f"q:{query}"] = {"ts": time.time(), "results": results}
        write_json(self.cache_path, prune_dict_by_age(cache, max_entries=120, max_age_hours=CACHE_HOURS))
        return results

    def candidates(self, query: str, context: set[str] | None = None) -> list[dict]:
        terms = query_terms(query)
        need = max(1, math.ceil(0.6 * len(terms)))
        qwords = {w.lower() for w in re.findall(r"[A-Za-z]+", query)}
        out = []
        for x in self.search(query):
            lic = (x.get("license") or "").lower()
            if lic not in ALLOWED or x.get("mature") or not x.get("url"):
                continue
            if (x.get("width") or 0) < 1000:
                continue
            title = (x.get("title") or "").strip()
            if {w.lower() for w in re.findall(r"[A-Za-z]+", title)} & (REJECT_TITLE_WORDS - qwords):
                continue
            tags = " ".join(t.get("name", "") for t in (x.get("tags") or []) if isinstance(t, dict))
            text = f"{title} {tags}"
            score = overlap(terms, text)
            if score < need:
                continue
            if not context_ok(query, context, text):
                continue
            label = {"by": f"CC BY {x.get('license_version') or ''}".strip(), "cc0": "CC0", "pdm": "Public Domain"}[lic]
            creator = re.sub(r"\s+", " ", x.get("creator") or "")[:60] or "Unknown author"
            out.append({"title": title or x["id"], "url": x["url"], "score": score, "width": x.get("width") or 0,
                        "height": x.get("height") or 0, "license": label, "creator": creator, "text": text,
                        "page_url": x.get("foreign_landing_url") or x.get("detail_url") or "", "date": "",
                        "credit": f"{creator} ({label})", "needs_credit": lic == "by",
                        "via": f"{(x.get('source') or 'Openverse').title()} (Openverse)"})
        out.sort(key=lambda c: (-c["score"], -min(c["width"], 3000)))
        return out
