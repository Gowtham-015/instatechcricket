"""Health state, circuit breaker, throttled alerts and secret-safe logging."""
from __future__ import annotations

import logging
import logging.handlers
import re
from datetime import datetime, timedelta, timezone

import requests

from . import config
from .storage import parse_iso, read_json, write_json

_TOKEN_PATTERNS = [re.compile(r"access_token=[^&\s\"']+"), re.compile(r"EAA[A-Za-z0-9]{20,}"),
                   re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+")]


def mask_secrets(text: str) -> str:
    for s in config.secrets_to_mask():
        text = text.replace(s, "***")
    for pat in _TOKEN_PATTERNS:
        text = pat.sub("***", text)
    return text


class SecretMaskFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_secrets(record.getMessage())
        record.args = ()
        return True


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if getattr(root, "_insta_configured", False):
        return
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            config.LOG_DIR / "agent.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    except OSError:
        pass
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(SecretMaskFilter())
        root.addHandler(h)
    root._insta_configured = True  # type: ignore[attr-defined]


class Health:
    def __init__(self, path=None, session=None, now_fn=None):
        self.path = path or (config.DATA_DIR / "health_state.json")
        self.http = session or requests
        self.now = now_fn or (lambda: datetime.now(timezone.utc))
        self.state = {"status": "NEW", "consecutive_failures": 0, "last_success": None,
                      "last_failure": None, "last_error": None, "paused_until": None, "last_alert": None,
                      **read_json(self.path, {})}

    def _save(self) -> None:
        write_json(self.path, self.state)

    def paused_until(self) -> datetime | None:
        pu = parse_iso(self.state.get("paused_until"))
        return pu if pu and pu > self.now() else None

    def record_success(self) -> None:
        self.state.update(status="ONLINE", consecutive_failures=0, last_success=self.now().isoformat(),
                          last_error=None, paused_until=None)
        self._save()

    def record_failure(self, error: str) -> None:
        n = int(self.state["consecutive_failures"]) + 1
        self.state.update(consecutive_failures=n, last_failure=self.now().isoformat(),
                          last_error=mask_secrets(error)[:300], status="DEGRADED")
        if n >= config.BREAKER_FAILURES:
            self.state["status"] = "PAUSED"
            self.state["paused_until"] = (self.now() + timedelta(hours=config.BREAKER_PAUSE_HOURS)).isoformat()
            self.alert(f"Circuit breaker opened after {n} consecutive failures. Last error: {self.state['last_error']}")
        self._save()

    def alert(self, message: str, *, force: bool = False) -> bool:
        """Throttled webhook alert (Slack/Discord/any JSON endpoint)."""
        if not config.ALERT_WEBHOOK_URL:
            return False
        last = parse_iso(self.state.get("last_alert"))
        if not force and last and self.now() - last < timedelta(hours=config.ALERT_THROTTLE_HOURS):
            return False
        text = mask_secrets(f"[{config.BRAND_NAME} Instagram agent] {message}")[:1500]
        try:
            self.http.post(config.ALERT_WEBHOOK_URL, json={"text": text, "content": text}, timeout=10)
        except requests.RequestException:
            return False
        self.state["last_alert"] = self.now().isoformat()
        self._save()
        return True
