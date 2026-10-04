#!/usr/bin/env python3
"""Instagram AI News Agent - command line entry point.

  python main.py verify            check config, Instagram account/quota, media host round-trip
  python main.py preview           build a real post from live news into ./preview (nothing is published)
  python main.py run               one publishing cycle (used by cron / GitHub Actions)
  python main.py run --force       ignore slot/interval/daily-cap (still honours dry-run)
  python main.py daemon            long-running scheduler (VPS / Docker)
  python main.py status            health, today's posts, next slots
  python main.py report            performance report from stored insights
  python main.py models            list Gemini models your key can use
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from core import config
from core.analytics import report
from core.health import Health, mask_secrets, setup_logging
from core.pipeline import Pipeline
from core.schedule import current_slot
from core.storage import parse_iso, read_json

log = logging.getLogger("main")


def token_hints() -> list[str]:
    t, host = config.IG_ACCESS_TOKEN, config.IG_API_HOST
    hints = []
    if any(ch.isspace() for ch in t):
        hints.append("IG_ACCESS_TOKEN contains spaces/line-breaks: paste it as ONE line.")
    if len(t) < 60:
        hints.append(f"IG_ACCESS_TOKEN is only {len(t)} characters: real tokens are 150+ characters, so it is probably cut off "
                     "or is not an access token (App ID / App Secret / user id).")
    if t.startswith("IG") and "graph.facebook.com" in host:
        hints.append("Your token starts with 'IG...' (Instagram Login). Set IG_API_HOST=https://graph.instagram.com")
    if t.startswith("EAA") and "graph.instagram.com" in host:
        hints.append("Your token starts with 'EAA...' (Facebook Login). Set IG_API_HOST=https://graph.facebook.com")
    if config.IG_USER_ID and not config.IG_USER_ID.isdigit():
        hints.append("IG_USER_ID must contain digits only.")
    return hints


def cmd_models(_args) -> int:
    from google import genai
    from core.ai_writer import pick_flash_model
    if not config.GEMINI_API_KEY:
        print("GEMINI_API_KEY is missing")
        return 1
    names = [m.name for m in genai.Client(api_key=config.GEMINI_API_KEY).models.list()]
    flash = sorted(n for n in names if "flash" in n)
    print("Flash models on your key:")
    for n in flash:
        print("  ", n.split("/")[-1])
    print("\nBest automatic choice:", pick_flash_model(names))
    return 0


def cmd_verify(_args) -> int:
    from PIL import Image
    from core.instagram_client import InstagramClient, InstagramError
    from core.media_host import get_host, wait_until_public
    problems = config.validate("publish")
    if problems:
        print("Configuration problems:")
        for p in problems:
            print("  -", p)
        return 1
    ok = True
    for h in token_hints():
        print("[HINT]", h)
    try:
        ig = InstagramClient()
        print(f"[OK] Instagram account: @{ig.verify_account()}")
        used, total = ig.publishing_usage()
        print(f"[OK] Publishing quota: {used}/{total} used in the last 24h")
    except InstagramError as e:
        ok = False
        print("[FAIL] Instagram:", mask_secrets(str(e)))
    try:
        import tempfile
        host = get_host()
        tmp = Path(tempfile.mkdtemp()) / "verify.jpg"
        Image.new("RGB", (320, 400), (30, 60, 120)).save(tmp, "JPEG")
        h = host.upload(tmp, f"verify_{int(datetime.now().timestamp())}.jpg")
        reachable = wait_until_public(h.url, attempts=5)
        host.delete(h.ref)
        print(f"[{'OK' if reachable else 'FAIL'}] Media host '{config.MEDIA_HOST}' round-trip")
        ok = ok and reachable
    except Exception as e:  # noqa: BLE001
        ok = False
        print("[FAIL] Media host:", mask_secrets(str(e)))
    from core.ai_writer import AIWriter
    w = AIWriter()
    if not w.ai_enabled:
        print("[WARN] GEMINI_API_KEY missing -> extractive fallback copy")
    else:
        try:
            out = w._generate('Return exactly this JSON and nothing else: {"ok": true}')
            print(f"[{'OK' if 'ok' in out else 'WARN'}] Gemini model '{w.model}' answered")
        except Exception as e:  # noqa: BLE001
            ok = False
            print("[FAIL] Gemini:", mask_secrets(str(e))[:250])
            print("       Run `python main.py models` to see the model names your key can use.")
    return 0 if ok else 1


def cmd_preview(_args) -> int:
    res = Pipeline().run_cycle(force=True, dry_run=True)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    if res.get("status") == "dry_run":
        print(f"\nOpen the files in {config.PREVIEW_DIR} to review the carousel + caption.")
    return 0 if res.get("status") in ("dry_run", "idle") else 1


def cmd_run(args) -> int:
    problems = config.validate("publish" if not (args.dry_run or config.DRY_RUN) else "dry")
    if problems:
        for p in problems:
            log.error("Config problem: %s", p)
        return 2
    res = Pipeline().run_cycle(force=args.force, dry_run=True if args.dry_run else None)
    log.info("Result: %s", json.dumps(res, ensure_ascii=False))
    print(json.dumps(res, ensure_ascii=False))
    return 1 if res.get("status") == "failed" else 0


def cmd_status(_args) -> int:
    h = Health()
    hist = read_json(config.DATA_DIR / "history.json", [])
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(timezone.utc)
    today = [x for x in hist if (t := parse_iso(x.get("published_at"))) and t.astimezone(tz).date() == now.astimezone(tz).date()]
    print(f"Status: {h.state['status']}   consecutive failures: {h.state['consecutive_failures']}")
    print(f"Last success: {h.state['last_success']}   paused until: {h.state['paused_until']}")
    if h.state["last_error"]:
        print("Last error:", h.state["last_error"])
    print(f"Posts today: {len(today)}/{config.MAX_POSTS_PER_DAY}   total tracked: {len(hist)}")
    sched = read_json(config.DATA_DIR / "schedule_state.json", {"done": {}})
    print("Slot now:", current_slot(now.astimezone(tz), config.POST_TIMES, config.SLOT_GRACE_MINUTES, set(sched["done"])) or "none due")
    print("Plan:", ", ".join(config.POST_TIMES), config.TIMEZONE)
    return 0


def cmd_report(_args) -> int:
    print(report(read_json(config.DATA_DIR / "history.json", [])))
    return 0


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"ok": True, "status": Health().state["status"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


def cmd_daemon(_args) -> int:
    from apscheduler.schedulers.blocking import BlockingScheduler
    problems = config.validate("publish")
    if problems:
        for p in problems:
            log.error("Config problem: %s", p)
        return 2
    port = os.getenv("PORT")
    if port:
        srv = HTTPServer(("0.0.0.0", int(port)), _HealthHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info("Health endpoint on :%s", port)
    interval = int(os.getenv("DAEMON_INTERVAL_MINUTES", "15"))

    def tick():
        try:
            Pipeline().run_cycle()
        except Exception:  # noqa: BLE001
            log.exception("Cycle crashed")

    sched = BlockingScheduler(timezone=config.TIMEZONE)
    sched.add_job(tick, "interval", minutes=interval, next_run_time=datetime.now(timezone.utc),
                  max_instances=1, coalesce=True)
    log.info("Daemon started: checking every %d min; plan %s %s", interval, config.POST_TIMES, config.TIMEZONE)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Stopped")
    return 0


def main(argv=None) -> int:
    setup_logging()
    ap = argparse.ArgumentParser(description="Instagram AI News Agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify")
    sub.add_parser("preview")
    r = sub.add_parser("run")
    r.add_argument("--force", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    sub.add_parser("daemon")
    sub.add_parser("status")
    sub.add_parser("report")
    sub.add_parser("models")
    args = ap.parse_args(argv)
    return {"verify": cmd_verify, "preview": cmd_preview, "run": cmd_run, "daemon": cmd_daemon,
            "status": cmd_status, "report": cmd_report, "models": cmd_models}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
