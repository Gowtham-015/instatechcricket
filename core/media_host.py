"""Public hosting for rendered JPEGs (Instagram fetches `image_url` itself, so URLs must be public).

cloudinary : recommended. Signed REST upload, deleted right after publishing.
github     : zero-signup option for PUBLIC repos. Files go to a dedicated `media` branch through the
             Contents API (never the main branch -> main stays tiny) and are deleted after publishing.
local      : you serve ./public/media over https yourself (VPS + nginx) and set MEDIA_BASE_URL.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from . import config

log = logging.getLogger("media_host")


@dataclass
class Hosted:
    url: str
    ref: dict  # provider specific handle used for deletion


class MediaHostError(RuntimeError):
    pass


class LocalHost:
    def __init__(self, base_url: str | None = None, root: Path | None = None):
        self.base_url = (base_url or config.MEDIA_BASE_URL).rstrip("/")
        self.root = Path(root or config.PUBLIC_DIR)

    def upload(self, path: Path, name: str) -> Hosted:
        self.root.mkdir(parents=True, exist_ok=True)
        dest = self.root / name
        shutil.copyfile(path, dest)
        return Hosted(f"{self.base_url}/{name}", {"path": str(dest)})

    def delete(self, ref: dict) -> None:
        Path(ref["path"]).unlink(missing_ok=True)


class CloudinaryHost:
    def __init__(self, cloud: str | None = None, key: str | None = None, secret: str | None = None, session=None):
        self.cloud = cloud or config.CLOUDINARY_CLOUD_NAME
        self.key = key or config.CLOUDINARY_API_KEY
        self.secret = secret or config.CLOUDINARY_API_SECRET
        self.http = session or requests

    def _sign(self, params: dict) -> str:
        to_sign = "&".join(f"{k}={params[k]}" for k in sorted(params) if params[k] not in ("", None))
        return hashlib.sha1((to_sign + self.secret).encode()).hexdigest()

    def upload(self, path: Path, name: str) -> Hosted:
        public_id = f"insta_agent/{Path(name).stem}"
        params = {"public_id": public_id, "timestamp": int(time.time())}
        data = {**params, "api_key": self.key, "signature": self._sign(params)}
        with open(path, "rb") as f:
            r = self.http.post(f"https://api.cloudinary.com/v1_1/{self.cloud}/image/upload",
                               data=data, files={"file": (name, f, "image/jpeg")}, timeout=60)
        if r.status_code != 200:
            try:
                reason = (r.json().get("error") or {}).get("message", "")
            except ValueError:
                reason = ""
            raise MediaHostError(f"Cloudinary upload failed: HTTP {r.status_code} {reason}".strip())
        url = r.json().get("secure_url", "")
        if not url.startswith("https://"):
            raise MediaHostError("Cloudinary returned no secure_url")
        if not url.lower().endswith((".jpg", ".jpeg")):  # IG accepts JPEG only -> force it
            url = url.rsplit(".", 1)[0] + ".jpg"
        return Hosted(url, {"public_id": public_id})

    def delete(self, ref: dict) -> None:
        params = {"public_id": ref["public_id"], "timestamp": int(time.time())}
        data = {**params, "api_key": self.key, "signature": self._sign(params)}
        self.http.post(f"https://api.cloudinary.com/v1_1/{self.cloud}/image/destroy", data=data, timeout=30)


class GitHubHost:
    API = "https://api.github.com"

    def __init__(self, token: str | None = None, repo: str | None = None, branch: str | None = None, session=None):
        self.token = token or config.GITHUB_TOKEN
        self.repo = repo or config.GITHUB_REPOSITORY
        self.branch = branch or config.MEDIA_BRANCH
        self.http = session or requests
        self._branch_ready = False

    @property
    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def _ensure_branch(self) -> None:
        if self._branch_ready:
            return
        r = self.http.get(f"{self.API}/repos/{self.repo}/git/ref/heads/{self.branch}", headers=self._h, timeout=30)
        if r.status_code == 404:
            meta = self.http.get(f"{self.API}/repos/{self.repo}", headers=self._h, timeout=30)
            if meta.status_code != 200:
                raise MediaHostError(f"GitHub repo lookup failed: HTTP {meta.status_code}")
            if meta.json().get("private"):
                raise MediaHostError("MEDIA_HOST=github needs a PUBLIC repo (Instagram must fetch the image)")
            default = meta.json()["default_branch"]
            base = self.http.get(f"{self.API}/repos/{self.repo}/git/ref/heads/{default}", headers=self._h, timeout=30)
            sha = base.json()["object"]["sha"]
            c = self.http.post(f"{self.API}/repos/{self.repo}/git/refs", headers=self._h, timeout=30,
                               json={"ref": f"refs/heads/{self.branch}", "sha": sha})
            if c.status_code not in (200, 201):
                raise MediaHostError(f"Could not create media branch: HTTP {c.status_code}")
        elif r.status_code != 200:
            raise MediaHostError(f"GitHub branch check failed: HTTP {r.status_code}")
        self._branch_ready = True

    def upload(self, path: Path, name: str) -> Hosted:
        self._ensure_branch()
        remote = f"media/{name}"
        payload = {"message": f"media {name}", "branch": self.branch,
                   "content": base64.b64encode(Path(path).read_bytes()).decode()}
        r = self.http.put(f"{self.API}/repos/{self.repo}/contents/{remote}", headers=self._h, json=payload, timeout=60)
        if r.status_code not in (200, 201):
            raise MediaHostError(f"GitHub upload failed: HTTP {r.status_code}")
        sha = r.json()["content"]["sha"]
        url = f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/{remote}"
        return Hosted(url, {"path": remote, "sha": sha})

    def delete(self, ref: dict) -> None:
        self.http.delete(f"{self.API}/repos/{self.repo}/contents/{ref['path']}", headers=self._h, timeout=30,
                         json={"message": f"cleanup {ref['path']}", "sha": ref["sha"], "branch": self.branch})

    def cleanup_old(self, max_age_hours: int = 48) -> int:
        """Delete leftovers from crashed runs (file names start with a UTC timestamp)."""
        r = self.http.get(f"{self.API}/repos/{self.repo}/contents/media", headers=self._h,
                          params={"ref": self.branch}, timeout=30)
        if r.status_code != 200:
            return 0
        cutoff = time.time() - max_age_hours * 3600
        n = 0
        for item in r.json():
            try:
                ts = time.mktime(time.strptime(item["name"][:14], "%Y%m%d%H%M%S")) - time.timezone
            except ValueError:
                continue
            if ts < cutoff:
                self.delete({"path": item["path"], "sha": item["sha"]})
                n += 1
        return n


def get_host(kind: str | None = None):
    kind = (kind or config.MEDIA_HOST).lower()
    if kind == "cloudinary":
        return CloudinaryHost()
    if kind == "github":
        return GitHubHost()
    if kind == "local":
        return LocalHost()
    raise MediaHostError(f"Unknown MEDIA_HOST '{kind}'")


def wait_until_public(url: str, *, attempts: int = 6, delay: float = 3.0, session=None, sleep=time.sleep) -> bool:
    """Instagram fetches the URL itself: verify it is reachable and really a JPEG before asking it to."""
    http = session or requests
    for i in range(attempts):
        try:
            r = http.get(url, timeout=20, stream=True)
            ctype = r.headers.get("Content-Type", "").lower()
            ok = r.status_code == 200 and ("image/jpeg" in ctype or "image/jpg" in ctype)
            r.close()
            if ok:
                return True
        except requests.RequestException:
            pass
        if i < attempts - 1:
            sleep(delay)
    return False
