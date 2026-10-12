"""Facebook Page publishing (same content as Instagram) through the Graph API.

* link post  : POST /{page}/feed  message + link  -> Facebook builds the preview card (the publisher's own picture,
               source name, click-through) exactly like a Telegram link preview.
* photos     : POST /{page}/photos (one photo) or unpublished photos + /feed attached_media (several)
* Reel       : POST /{page}/video_reels  start -> upload via hosted file_url -> finish (video_state=PUBLISHED)
Needs a PAGE access token with pages_manage_posts (Facebook Login, not the Instagram Login token).
Writes are never retried blindly: a lost response could otherwise create a duplicate post.
"""
from __future__ import annotations

import json
import logging
import time

import requests

from . import config

log = logging.getLogger("facebook")


class FacebookError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, transient: bool = False):
        super().__init__(message)
        self.code, self.transient = code, transient

    @property
    def token_problem(self) -> bool:
        return self.code in (190, 102, 10, 200, 283)


NEEDED_SCOPES = ("pages_manage_posts", "pages_read_engagement")


def token_problems(info: dict, page_id: str) -> list[str]:
    """Plain-language reasons why posting will fail, from FacebookPageClient.diagnose()."""
    out: list[str] = []
    if not info.get("valid"):
        out.append("The token is invalid or expired. Generate a new user token and run `python main.py fb-pages ...` again.")
        return out
    if info.get("type") and info["type"] != "PAGE":
        out.append(f"FB_PAGE_ACCESS_TOKEN is a {info['type']} token. It must be a PAGE token: run `python main.py fb-pages ...` "
                   "and paste the Page's FB_PAGE_ACCESS_TOKEN line (not your user token).")
    if info.get("profile_id") and page_id and info["profile_id"] != str(page_id):
        out.append(f"The token belongs to Page {info['profile_id']}, but FB_PAGE_ID is {page_id}.")
    missing = [x for x in NEEDED_SCOPES if x not in (info.get("scopes") or [])]
    if missing:
        out.append("The token lacks " + ", ".join(missing) + ". Generate the user token again with those permissions "
                   "(and tick your Page in the popup), then re-run `python main.py fb-pages ...`.")
    if info.get("can_post") is False:
        out.append("Facebook says this token cannot post to the Page. In Meta Business settings give your account FULL control "
                   "of the Page (Pages > your Page > People) and add the app as an asset (Accounts > Apps > your app > "
                   "Add assets > Pages), then create a new Page token.")
    if info.get("page_error"):
        out.append("Reading the Page failed: " + info["page_error"])
    exp = info.get("expires_at")
    if exp:
        out.append("This token expires. Re-run `python main.py fb-pages` with --app-id and --app-secret for one that never expires.")
    return out


class FacebookPageClient:
    def __init__(self, page_id: str | None = None, token: str | None = None, version: str | None = None,
                 session=None, sleep=time.sleep, clock=time.monotonic):
        self.page_id = page_id or config.FB_PAGE_ID
        self.token = token or config.FB_PAGE_ACCESS_TOKEN
        v = version or config.FB_API_VERSION
        self.base = f"https://graph.facebook.com/{v}"
        self.rupload = f"https://rupload.facebook.com/video-upload/{v}"
        self.http = session or requests
        self._sleep, self._clock = sleep, clock

    # ------------------------------------------------------------ transport
    @staticmethod
    def _parse(r) -> dict:
        try:
            body = r.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        if r.status_code >= 400 or "error" in body:
            err = body.get("error", {}) if isinstance(body.get("error"), dict) else {}
            raise FacebookError(err.get("message", f"HTTP {r.status_code}"), code=err.get("code"),
                                transient=bool(err.get("is_transient")) or r.status_code in (429, 500, 502, 503, 504))
        return body

    def _post(self, path: str, data: dict) -> dict:
        try:
            r = self.http.post(f"{self.base}/{path.lstrip('/')}", data={**data, "access_token": self.token}, timeout=90)
        except requests.RequestException as e:
            raise FacebookError(f"network error on {path}: {type(e).__name__}", transient=True) from e
        return self._parse(r)

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            r = self.http.get(f"{self.base}/{path.lstrip('/')}", params={**(params or {}), "access_token": self.token},
                              timeout=40)
        except requests.RequestException as e:
            raise FacebookError(f"network error on {path}: {type(e).__name__}", transient=True) from e
        return self._parse(r)

    # ------------------------------------------------------------ read
    def verify(self) -> str:
        return self._get(self.page_id, {"fields": "name"}).get("name", self.page_id)

    def diagnose(self) -> dict:
        """What Facebook says about the configured token (a token may inspect itself) and the Page."""
        data = self._get("debug_token", {"input_token": self.token}).get("data", {}) or {}
        info = {"valid": bool(data.get("is_valid")), "type": str(data.get("type", "")).upper(),
                "scopes": list(data.get("scopes") or []), "profile_id": str(data.get("profile_id") or ""),
                "expires_at": data.get("expires_at"), "app": data.get("application", "")}
        try:
            page = self._get(self.page_id, {"fields": "name,can_post"})
            info["page_name"], info["can_post"] = page.get("name", ""), page.get("can_post")
        except FacebookError as e:
            info["page_error"] = str(e)[:160]
        return info

    @staticmethod
    def list_pages(user_token: str, session=None) -> list[dict]:
        """Pages the user manages, each with its own (non-expiring) Page token. Used by `main.py fb-pages`."""
        r = (session or requests).get(f"https://graph.facebook.com/{config.FB_API_VERSION}/me/accounts", timeout=30,
                                      params={"fields": "id,name,access_token,tasks", "access_token": user_token})
        return FacebookPageClient._parse(r).get("data", [])

    @staticmethod
    def page_by_id(page_id: str, user_token: str, session=None) -> dict | None:
        """One Page's token fetched directly by ID. Works for Pages owned through a Business Portfolio, where
        /me/accounts is often empty. Returns None when the user has no access to that Page."""
        r = (session or requests).get(f"https://graph.facebook.com/{config.FB_API_VERSION}/{page_id}", timeout=30,
                                      params={"fields": "id,name,access_token", "access_token": user_token})
        data = FacebookPageClient._parse(r)
        return data if data.get("access_token") else None

    @staticmethod
    def long_lived_user_token(short_token: str, app_id: str, app_secret: str, session=None) -> str:
        """Swap a short-lived user token (~1h) for a long-lived one (~60 days). Page tokens read with a long-lived
        user token never expire."""
        r = (session or requests).get(f"https://graph.facebook.com/{config.FB_API_VERSION}/oauth/access_token", timeout=30,
                                      params={"grant_type": "fb_exchange_token", "client_id": app_id,
                                              "client_secret": app_secret, "fb_exchange_token": short_token})
        token = FacebookPageClient._parse(r).get("access_token")
        if not token:
            raise FacebookError("Facebook returned no long-lived token")
        return token

    @staticmethod
    def token_expiry(token: str, app_id: str, app_secret: str, session=None) -> int | None:
        """Unix expiry of a token (0 = never expires), or None when it cannot be checked."""
        try:
            r = (session or requests).get(f"https://graph.facebook.com/{config.FB_API_VERSION}/debug_token", timeout=30,
                                          params={"input_token": token, "access_token": f"{app_id}|{app_secret}"})
            data = FacebookPageClient._parse(r).get("data", {})
        except FacebookError:
            return None
        exp = data.get("expires_at")
        return int(exp) if isinstance(exp, (int, float)) else None

    # ------------------------------------------------------------ publish
    @staticmethod
    def _link(post_id: str) -> str:
        return f"https://www.facebook.com/{post_id}"

    def post_link(self, message: str, link: str) -> dict:
        res = self._post(f"{self.page_id}/feed", {"message": message, "link": link})
        return {"id": res["id"], "permalink": self._link(res["id"])}

    def post_photos(self, urls: list[str], message: str) -> dict:
        if not urls:
            raise FacebookError("no photos to post")
        if len(urls) == 1:
            res = self._post(f"{self.page_id}/photos", {"url": urls[0], "message": message, "published": "true"})
            pid = res.get("post_id") or res["id"]
            return {"id": pid, "permalink": self._link(pid)}
        ids = [self._post(f"{self.page_id}/photos", {"url": u, "published": "false"})["id"] for u in urls]

        def feed(selected: list[str]) -> dict:
            data = {"message": message}
            data.update({f"attached_media[{i}]": json.dumps({"media_fbid": m}) for i, m in enumerate(selected)})
            return self._post(f"{self.page_id}/feed", data)
        try:
            res = feed(ids)
        except FacebookError as e:
            if e.token_problem or len(ids) <= 4:
                raise
            log.info("Facebook rejected %d photos in one post -> retrying with the first 4", len(ids))
            res = feed(ids[:4])
        return {"id": res["id"], "permalink": self._link(res["id"])}

    def post_reel(self, video_url: str, description: str, wait_seconds: float = 240.0) -> dict:
        start = self._post(f"{self.page_id}/video_reels", {"upload_phase": "start"})
        vid = start.get("video_id")
        if not vid:
            raise FacebookError("reel upload session returned no video_id")
        try:
            r = self.http.post(start.get("upload_url") or f"{self.rupload}/{vid}", timeout=300,
                               headers={"Authorization": f"OAuth {self.token}", "file_url": video_url})
        except requests.RequestException as e:
            raise FacebookError(f"network error during reel upload: {type(e).__name__}", transient=True) from e
        if not self._parse(r).get("success"):
            raise FacebookError("Facebook did not accept the hosted video (is the URL public and not blocked by robots.txt?)")
        self._post(f"{self.page_id}/video_reels", {"video_id": vid, "upload_phase": "finish",
                                                    "video_state": "PUBLISHED", "description": description})
        self._wait_reel(vid, wait_seconds)
        return {"id": vid, "permalink": f"https://www.facebook.com/reel/{vid}"}

    def _wait_reel(self, vid: str, timeout: float) -> None:
        """Best effort: surface processing errors, but a slow Reel is not a failure."""
        start = self._clock()
        while self._clock() - start < timeout:
            try:
                st = (self._get(vid, {"fields": "status"}).get("status") or {})
            except FacebookError:
                return
            state = st.get("video_status", "")
            if state == "ready":
                return
            if state == "error":
                raise FacebookError("Facebook could not process the Reel: " + json.dumps(st)[:200])
            self._sleep(5)
        log.info("Facebook is still processing the Reel (it will appear on the Page shortly)")
