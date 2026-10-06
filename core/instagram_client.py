"""Minimal, strict client for the Instagram Graph API content-publishing flow.

Flow (carousel):  child containers (is_carousel_item) -> wait FINISHED -> parent CAROUSEL container
                  -> wait FINISHED -> media_publish.
Facts verified against Meta docs: JPEG only, public image_url, containers expire after 24h,
API-published posts are rate limited (we also read /content_publishing_limit).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from . import config

log = logging.getLogger("instagram")
TRANSIENT_HTTP = {429, 500, 502, 503, 504}


class InstagramError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, subcode: int | None = None,
                 transient: bool = False, ambiguous: bool = False):
        super().__init__(message)
        self.code, self.subcode, self.transient, self.ambiguous = code, subcode, transient, ambiguous

    @property
    def token_problem(self) -> bool:
        return self.code in (190, 102, 10, 200)


@dataclass
class PublishResult:
    media_id: str
    permalink: str | None


class InstagramClient:
    def __init__(self, user_id: str | None = None, token: str | None = None, host: str | None = None,
                 version: str | None = None, session=None, poll_interval: float = 3.0,
                 poll_timeout: float = 150.0, sleep=time.sleep, clock=time.monotonic):
        self.user_id = user_id or config.IG_USER_ID
        self.token = token or config.IG_ACCESS_TOKEN
        self.base = f"{(host or config.IG_API_HOST).rstrip('/')}/{version or config.IG_API_VERSION}"
        self.http = session or requests
        self.poll_interval, self.poll_timeout = poll_interval, poll_timeout
        self._sleep, self._clock = sleep, clock

    # ------------------------------------------------------------ transport
    def _call(self, method: str, path: str, params: dict | None = None, *, retries: int = 3,
              mutating: bool = False) -> dict:
        url = f"{self.base}/{path.lstrip('/')}"
        payload = {**(params or {}), "access_token": self.token}
        last: InstagramError | None = None
        for attempt in range(retries):
            try:
                if method == "GET":
                    r = self.http.get(url, params=payload, timeout=40)
                else:
                    r = self.http.post(url, data=payload, timeout=60)
            except requests.Timeout as e:
                last = InstagramError(f"timeout calling {path}", transient=True, ambiguous=mutating)
                if mutating:       # never blindly repeat a write that may have succeeded
                    raise last from e
            except requests.RequestException as e:
                last = InstagramError(f"network error calling {path}: {type(e).__name__}", transient=True)
            else:
                body = self._json(r)
                if r.status_code < 400 and "error" not in body:
                    return body
                err = body.get("error", {}) if isinstance(body, dict) else {}
                last = InstagramError(
                    f"{path}: {err.get('message', f'HTTP {r.status_code}')}",
                    code=err.get("code"), subcode=err.get("error_subcode"),
                    transient=bool(err.get("is_transient")) or r.status_code in TRANSIENT_HTTP or err.get("code") in (1, 2, 4, 17, 341),
                    ambiguous=mutating and r.status_code >= 500)
                if not last.transient or last.ambiguous:
                    raise last
            if attempt < retries - 1:
                self._sleep(2.0 * (2 ** attempt))
        assert last is not None
        raise last

    @staticmethod
    def _json(r) -> dict:
        try:
            data = r.json()
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    # ------------------------------------------------------------ read-only
    def verify_account(self) -> str:
        data = self._call("GET", self.user_id, {"fields": "username"})
        return data.get("username") or data.get("id", "")

    def publishing_usage(self) -> tuple[int, int]:
        """(posts used in the rolling 24h window, allowed)."""
        data = self._call("GET", f"{self.user_id}/content_publishing_limit", {"fields": "quota_usage,config"})
        item = (data.get("data") or [{}])[0]
        total = (item.get("config") or {}).get("quota_total", config.IG_DAILY_API_LIMIT)
        return int(item.get("quota_usage", 0)), int(total)

    def container_info(self, container_id: str) -> dict:
        return self._call("GET", container_id, {"fields": "status_code,status"})

    def container_status(self, container_id: str) -> str:
        return self.container_info(container_id).get("status_code", "")

    def permalink(self, media_id: str) -> str | None:
        try:
            return self._call("GET", media_id, {"fields": "permalink"}, retries=2).get("permalink")
        except InstagramError:
            return None

    def recent_media(self, limit: int = 8) -> list[dict]:
        return self._call("GET", f"{self.user_id}/media", {"fields": "id,caption,permalink,timestamp", "limit": limit}).get("data", [])

    def insights(self, media_id: str) -> dict:
        data = self._call("GET", f"{media_id}/insights",
                          {"metric": "reach,saved,shares,likes,comments,total_interactions"}, retries=2)
        return {m["name"]: (m.get("values") or [{}])[0].get("value", 0) for m in data.get("data", [])}

    # ------------------------------------------------------------ publishing
    def _create(self, params: dict) -> str:
        cid = self._call("POST", f"{self.user_id}/media", params, mutating=True).get("id")
        if not cid:
            raise InstagramError("container creation returned no id")
        return cid

    def wait_ready(self, container_id: str, *, timeout: float | None = None, interval: float | None = None) -> None:
        timeout = self.poll_timeout if timeout is None else timeout
        interval = self.poll_interval if interval is None else interval
        start = self._clock()
        while True:
            info = self.container_info(container_id)
            status = info.get("status_code", "")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                detail = str(info.get("status", ""))[:200]
                raise InstagramError(f"container {container_id} status {status} {detail}".strip())
            if self._clock() - start > timeout:
                raise InstagramError(f"container {container_id} not ready after {int(timeout)}s "
                                     f"(last status {status or 'unknown'})", transient=True)
            self._sleep(interval)

    def create_carousel(self, image_urls: list[str], caption: str) -> str:
        if not 2 <= len(image_urls) <= 10:
            raise InstagramError("carousel needs 2-10 images")
        children = []
        for url in image_urls:
            cid = self._create({"image_url": url, "is_carousel_item": "true"})
            children.append(cid)
        for cid in children:
            self.wait_ready(cid)
        parent = self._create({"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption})
        self.wait_ready(parent)
        return parent

    def create_single(self, image_url: str, caption: str, alt_text: str | None = None) -> str:
        params = {"image_url": image_url, "caption": caption}
        if alt_text:
            params["alt_text"] = alt_text[:1000]
        cid = self._create(params)
        self.wait_ready(cid)
        return cid

    def publish_container(self, container_id: str) -> PublishResult:
        media_id = self._call("POST", f"{self.user_id}/media_publish", {"creation_id": container_id},
                              mutating=True, retries=1).get("id")
        if not media_id:
            raise InstagramError("media_publish returned no id")
        return PublishResult(media_id, self.permalink(media_id))

    def find_published_by_caption(self, caption: str) -> PublishResult | None:
        """Recovery after an ambiguous publish error: did Instagram actually publish it?"""
        head = caption[:80]
        try:
            for m in self.recent_media():
                if (m.get("caption") or "").startswith(head):
                    return PublishResult(m["id"], m.get("permalink"))
        except InstagramError:
            pass
        return None

    def create_reel(self, video_url: str, caption: str, share_to_feed: bool = True) -> str:
        cid = self._create({"media_type": "REELS", "video_url": video_url, "caption": caption,
                            "share_to_feed": "true" if share_to_feed else "false"})
        self.wait_ready(cid, timeout=config.IG_VIDEO_POLL_TIMEOUT, interval=5.0)  # video needs processing time
        return cid

    def _publish_with_recovery(self, container: str, caption: str) -> PublishResult:
        try:
            return self.publish_container(container)
        except InstagramError as e:
            if e.ambiguous:
                self._sleep(5)
                found = self.find_published_by_caption(caption)
                if found:
                    log.warning("Publish response lost but post exists on Instagram; recovered")
                    return found
            raise

    def publish_images(self, image_urls: list[str], caption: str) -> PublishResult:
        container = (self.create_single(image_urls[0], caption) if len(image_urls) == 1
                     else self.create_carousel(image_urls, caption))
        return self._publish_with_recovery(container, caption)

    def publish_reel(self, video_url: str, caption: str, share_to_feed: bool | None = None) -> PublishResult:
        share = config.REEL_SHARE_TO_FEED if share_to_feed is None else share_to_feed
        return self._publish_with_recovery(self.create_reel(video_url, caption, share), caption)
