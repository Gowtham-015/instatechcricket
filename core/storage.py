"""Crash-safe JSON persistence, retention helpers and a stale-aware lock."""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, data: Any) -> None:
    """Atomic write: temp file in same dir + os.replace (never leaves half files)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def prune_records(records: list[dict], *, max_records: int, max_days: int,
                  time_key: str = "published_at") -> list[dict]:
    """Drop records older than max_days, then keep only the newest max_records."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
    kept = []
    for r in records:
        ts = parse_iso(r.get(time_key))
        if ts is None or ts >= cutoff:
            kept.append(r)
    return kept[-max_records:]


def prune_dict_by_age(d: dict[str, dict], *, max_entries: int, max_age_hours: float,
                      time_key: str = "ts") -> dict[str, dict]:
    cutoff = time.time() - max_age_hours * 3600
    alive = {k: v for k, v in d.items() if float(v.get(time_key, 0)) >= cutoff}
    if len(alive) > max_entries:
        newest = sorted(alive.items(), key=lambda kv: float(kv[1].get(time_key, 0)), reverse=True)
        alive = dict(newest[:max_entries])
    return alive


class LockError(RuntimeError):
    pass


class ProcessLock:
    """File lock with TTL-based stale detection (Windows/Linux safe: no os.kill)."""

    def __init__(self, path: Path, ttl_minutes: int = 30):
        self.path = Path(path)
        self.ttl = ttl_minutes * 60
        self._held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            age = time.time() - self.path.stat().st_mtime
            if age < self.ttl:
                raise LockError(f"Another run holds {self.path.name} (age {int(age)}s)")
            self.path.unlink(missing_ok=True)  # stale -> self-heal
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as e:
            raise LockError("Lock race lost") from e
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        self._held = True

    def release(self) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def retry(fn: Callable[[], Any], *, attempts: int = 3, base_delay: float = 1.5,
          retry_on: tuple = (Exception,), sleep: Callable[[float], None] = time.sleep) -> Any:
    """Exponential backoff helper."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except retry_on as e:  # noqa: PERF203
            last = e
            if i < attempts - 1:
                sleep(base_delay * (2 ** i))
    assert last is not None
    raise last
