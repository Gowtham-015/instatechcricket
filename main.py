#!/usr/bin/env python3
"""Instagram AI News Agent - command line entry point.

  python main.py verify            check config, Instagram account/quota, media host round-trip
  python main.py preview [--format photo|carousel|reel|video|card]   build a real post into ./preview (nothing is published)
  python main.py explain [--last N]   why the last posts got their format and media (data/decisions.jsonl)
  python main.py run               one publishing cycle (used by cron / GitHub Actions)
  python main.py run --force       ignore slot/interval/daily-cap (still honours dry-run)
  python main.py run --force --format reel   publish one Reel now
  python main.py daemon            long-running scheduler (VPS / Docker)
  python main.py status            health, today's posts, next slots
  python main.py report            performance report from stored insights
  python main.py models            list Gemini models your key can use
  python main.py clips             show the footage clips found in BROLL_DIR
  python main.py trending          Google Trends + how today's news ranks (nothing is posted)
  python main.py remix --topic cricket   script + caption + checklist for an in-app Remix of an official clip
  python main.py post-clip --video f.mp4 --topic "..." --credit "Owner" --confirm-rights   post YOUR licensed clip
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


def cmd_clips(_args) -> int:
    from core.feeds import CATEGORY_STYLE
    d = Path(config.BROLL_DIR) if config.BROLL_DIR else None
    if not d or not d.is_dir():
        print("BROLL_DIR is not set or the folder does not exist.")
        print("1) Create a folder, e.g.  C:\\Users\\you\\broll  with sub-folders:  " + ", ".join(CATEGORY_STYLE))
        print("2) Put vertical (or landscape) .mp4 clips in them  3) Set BROLL_DIR=<that folder> in .env")
        return 1
    ext = {".mp4", ".mov", ".m4v"}
    total = 0
    print(f"BROLL_DIR = {d}")
    for cat in CATEGORY_STYLE:
        sub = next((x for x in d.iterdir() if x.is_dir() and x.name.lower() == cat.lower()), None)
        n = len([p for p in sub.iterdir() if p.suffix.lower() in ext]) if sub else 0
        total += n
        print(f"  {cat:<14} {n:>3} clips" + ("   <- add at least 6 for variety" if n < 6 else ""))
    loose = len([p for p in d.iterdir() if p.is_file() and p.suffix.lower() in ext])
    print(f"  (loose files in the main folder, used for any category: {loose})")
    return 0 if (total or loose) else 1


def cmd_post_clip(args) -> int:
    from core.clip_post import ClipError, post_clip
    if not args.confirm_rights:
        print("Refusing to post: add --confirm-rights to confirm you own this footage or have the owner's permission/"
              "licence to publish it.\nThe agent never downloads other people's videos; broadcaster/federation clips "
              "need a licence or written permission, otherwise Instagram can remove the post and strike the account.")
        return 2
    dry = bool(args.dry_run or config.DRY_RUN)
    problems = config.validate("dry" if dry else "publish")
    if problems:
        for p in problems:
            print("Config problem:", p)
        return 2
    try:
        res = post_clip(Pipeline(), video=Path(args.video), topic=args.topic, credit=args.credit, notes=args.notes or "",
                        category=args.category, dry=dry, from_news=not args.no_news)
    except (ClipError, Exception) as e:  # noqa: BLE001
        print("FAILED:", mask_secrets(str(e))[:300])
        return 1
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 1 if res.get("status") == "failed" else 0


def cmd_footage(args) -> int:
    """Show which search terms and clips the agent would use for a topic (nothing is downloaded or posted)."""
    from core.stock import configured_providers, local_clips, overlap, query_terms, queries_for
    qs = queries_for(args.category, [], args.topic)
    print("Search terms:", " | ".join(qs[:8]))
    for px in configured_providers():
        print(f"\n[{px.name}]")
        for q in qs[:5]:
            try:
                items = px._items(q)
            except Exception as e:  # noqa: BLE001
                print(f"  {q}: ERROR {mask_secrets(str(e))[:100]}")
                continue
            terms = query_terms(q)
            ranked = sorted(((overlap(terms, i.get('text', '')), i.get('text', '')[:55]) for i in items), reverse=True)
            ok = [r for r in ranked if r[0] >= 1]
            print(f"  {q}: {len(ok)} relevant of {len(items)}" + (f"   e.g. {ok[0][1]}" if ok else "   (would be skipped)"))
    mine = local_clips(config.BROLL_DIR, 5, args.category, queries=qs)
    if mine:
        print("\n[your clips]", ", ".join(p.name for p in mine))
    return 0


def cmd_trending(_args) -> int:
    """What Google says is trending, and how today's collected news ranks against it (nothing is posted)."""
    from core.collector import collect_news
    from core.dedupe import dedupe_batch
    from core.ranker import build_clusters
    from core.trends import fetch_trends
    trends = fetch_trends()
    print(f"Google Trends ({config.TRENDS_GEO}): " + (", ".join(f"{t.query} ({t.traffic:,}+)" for t in trends) or "unavailable"))
    clusters = build_clusters(dedupe_batch(collect_news()), trends=trends)
    shown = [c for c in clusters if not c["digest"]][:12]
    print(f"\nTop stories (MIN_SOURCES={config.MIN_SOURCES}; digest/roundup items are excluded):")
    for c in shown:
        ok = len(c["sources"]) >= config.MIN_SOURCES or c["trend"]
        print(f"  {c['score']:5.1f}  {len(c['sources'])} src  {'TREND:' + c['trend'] if c['trend'] else '':<22}"
              f"{'' if ok else '[too little coverage] '}{c['lead']['title'][:75]}")
    return 0


def cmd_remix(args) -> int:
    """Prepare everything for an in-app Remix of an official clip: story, script, headline, caption, checklist."""
    from core.drafts import build_draft
    path = build_draft(Pipeline(), category=args.category, topic=args.topic)
    if not path:
        print("No suitable trending story found for that filter (try without --topic, or run: python main.py trending).")
        return 1
    print(path.read_text(encoding="utf-8"))
    print(f"\nSaved to {path}")
    return 0


def save_env_values(path: Path, values: dict[str, str]) -> None:
    """Set KEY=value lines in a .env file (replace the existing line, or append), keeping everything else untouched."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done: set[str] = set()
    out = []
    for ln in lines:
        key = ln.split("=", 1)[0].strip() if "=" in ln and not ln.lstrip().startswith("#") else None
        if key in values:
            if key not in done:
                out.append(f"{key}={values[key]}")
                done.add(key)
            continue          # drop duplicate old lines of the same key
        out.append(ln)
    for k, v in values.items():
        if k not in done:
            out.append(f"{k}={v}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def cmd_fb_pages(args) -> int:
    """List the Facebook Pages you manage with their Page tokens, ready to paste into .env."""
    import time as _time
    from core.facebook_client import FacebookError, FacebookPageClient
    user_token = args.user_token
    long_lived = bool(args.app_id and args.app_secret)
    try:
        if long_lived:
            user_token = FacebookPageClient.long_lived_user_token(user_token, args.app_id, args.app_secret)
            print("Exchanged for a long-lived user token (~60 days): Page tokens read with it do not expire.")
        pages = FacebookPageClient.list_pages(user_token)
        if not pages and args.page_id:
            print("The Pages list is empty (normal for Pages owned through a Business Portfolio); asking for the Page directly.")
            one = FacebookPageClient.page_by_id(args.page_id, user_token)
            pages = [one] if one else []
    except FacebookError as e:
        print("FAILED:", mask_secrets(str(e))[:200])
        return 1
    if not pages:
        print("No Pages found. The token needs pages_show_list and you must be an admin of the Page.")
        if not args.page_id:
            print("Pages owned through a Business Portfolio are not listed. Re-run with --page-id <your Page ID>.")
        else:
            print("Facebook gave no token for that Page ID: the account must have full control (or Create content) of the Page,")
            print("including in Meta Business Suite > Settings > Business assets > Pages.")
        return 1
    if args.save:
        chosen = [pg for pg in pages if not args.page_id or str(pg["id"]) == str(args.page_id)]
        if len(chosen) != 1 or not chosen[0].get("access_token"):
            print("--save needs exactly one Page: add --page-id <your Page ID>.")
            for pg in pages:
                print(f"  {pg['name']}  {pg['id']}")
            return 1
        pg = chosen[0]
        env_path = config.BASE_DIR / ".env"
        save_env_values(env_path, {"FB_PAGE_ID": str(pg["id"]), "FB_PAGE_ACCESS_TOKEN": pg["access_token"]})
        print(f"Saved FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN for '{pg['name']}' into {env_path} (the token is not shown).")
        if long_lived:
            exp = FacebookPageClient.token_expiry(pg["access_token"], args.app_id, args.app_secret)
            print("Page token expires:", "never" if exp == 0 else (_time.strftime("%Y-%m-%d", _time.gmtime(exp)) if exp else "unknown"))
        else:
            print("WARNING: without --app-id and --app-secret this token may expire within hours or days.")
        print("Next: python main.py fb-check")
        return 0
    for pg in pages:
        if "tasks" in pg:
            can = "CREATE_CONTENT" in (pg.get("tasks") or [])
            note = f"  (can publish: {'yes' if can else 'NO - need Page admin/editor role'})"
        else:
            note = ""
        print(f"\n{pg['name']}{note}")
        print(f"FB_PAGE_ID={pg['id']}\nFB_PAGE_ACCESS_TOKEN={pg.get('access_token', '')}")
        if long_lived and pg.get("access_token"):
            exp = FacebookPageClient.token_expiry(pg["access_token"], args.app_id, args.app_secret)
            if exp == 0:
                print("Page token expires: never")
            elif exp:
                print(f"Page token expires: {_time.strftime('%Y-%m-%d', _time.gmtime(exp))}")
    if not long_lived:
        print("\nWARNING: without --app-id and --app-secret this Page token may expire within hours or days.")
        print("Re-run with both (App settings > Basic) to get a token that never expires.")
    print("\nPaste ONE Page's two lines into .env (keep the token secret). Then run: python main.py verify")
    return 0


def cmd_fb_check(_args) -> int:
    """Explain, in plain words, why Facebook posting works or fails with the token in .env."""
    from core.facebook_client import FacebookError, FacebookPageClient, token_problems
    if not (config.FB_PAGE_ID and config.FB_PAGE_ACCESS_TOKEN):
        print("FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN are not set in .env (see README 'Facebook Page').")
        return 1
    try:
        info = FacebookPageClient().diagnose()
    except FacebookError as e:
        print("FAILED:", mask_secrets(str(e))[:200])
        return 1
    print(f"Token type: {info['type'] or 'unknown'} | valid: {info['valid']} | Page: {info.get('page_name', '?')} "
          f"({info['profile_id'] or config.FB_PAGE_ID})")
    print("Permissions:", ", ".join(info["scopes"]) or "none")
    print("Can post to the Page:", info.get("can_post", "unknown"))
    exp = info.get("expires_at")
    print("Expires:", "never" if exp == 0 else (exp or "unknown"))
    problems = token_problems(info, config.FB_PAGE_ID)
    if not problems:
        print("\nRESULT: the token looks right. If a post still fails, the Page may not have this app as an asset: Business settings > "
              "Accounts > Apps > your app > Add assets > Pages (full control).")
        return 0
    print("\nRESULT: fix this first:")
    for p in problems:
        print(" -", p)
    return 1


def cmd_sources(_args) -> int:
    """Test every news feed (built-in + EXTRA_FEEDS): how many items it returns and how many are fresh."""
    from datetime import timedelta
    from core.collector import fetch_feed
    from core.feeds import all_feeds
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.MAX_NEWS_AGE_HOURS)
    bad = 0
    for cat, feeds in all_feeds().items():
        print(f"\n{cat}")
        for f in feeds:
            try:
                items = fetch_feed(f, cat)
                fresh = sum(1 for a in items if a["published"] and datetime.fromisoformat(a["published"]) >= cutoff)
                print(f"  [OK]   {f['name']:<30} {len(items):>3} items, {fresh:>3} fresh")
            except Exception as e:  # noqa: BLE001
                bad += 1
                print(f"  [FAIL] {f['name']:<30} {type(e).__name__}: {mask_secrets(str(e))[:80]}")
    return 1 if bad else 0


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
    if config.FB_PAGE_ID and config.FB_PAGE_ACCESS_TOKEN and config.FB_MODE != "off":
        from core.facebook_client import FacebookError, FacebookPageClient
        try:
            print(f"[OK] Facebook Page: {FacebookPageClient().verify()} (mode: {config.FB_MODE})")
            try:
                from core.facebook_client import token_problems
                for prob in token_problems(FacebookPageClient().diagnose(), config.FB_PAGE_ID):
                    print("[WARN] Facebook posting will likely fail:", prob)
            except FacebookError:
                pass
        except FacebookError as e:
            ok = False
            print("[FAIL] Facebook:", mask_secrets(str(e))[:200])
            if e.token_problem:
                print("       Use a PAGE token from Facebook Login with pages_manage_posts: python main.py fb-pages --help")
    else:
        print("[INFO] Facebook cross-posting not configured (optional): see README 'Facebook Page'")
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
    from core.video_renderer import encoder_available, find_ffmpeg
    if encoder_available():
        print(f"[OK] ffmpeg ready for Reels ({find_ffmpeg()})")
    elif "reel" in config.CONTENT_FORMATS:
        print("[WARN] ffmpeg not found -> Reels will be skipped (carousels only). Fix: pip install imageio-ffmpeg")
    if "reel" in config.CONTENT_FORMATS and config.REEL_STYLE == "dynamic":
        from core.stock import configured_providers, local_clips
        from core.tts import synthesize
        providers = configured_providers()
        for px in providers:
            try:
                n = len(px._items("city street"))
                print(f"[{'OK' if n else 'WARN'}] {px.name} footage search returned {n} usable clips")
            except Exception as e:  # noqa: BLE001
                print(f"[FAIL] {px.name}:", mask_secrets(str(e))[:200])
        mine = local_clips(config.BROLL_DIR, 99)
        if mine:
            print(f"[OK] {len(mine)} of your own clips found in BROLL_DIR (run `python main.py clips` for details)")
        if not providers and not mine:
            print("[WARN] No footage source configured -> Reels use an animated background. Options: add a free "
                  "PIXABAY_API_KEY, or put clips in BROLL_DIR (see README 'Footage without any API key')")
        if config.PHOTO_DIR:
            from core.localphotos import LocalPhotos
            lp = LocalPhotos()
            n = len([f for f in lp.dir.rglob("*") if f.suffix.lower() in {".jpg", ".jpeg", ".png"}]) if lp.enabled else 0
            print(f"[{'OK' if n else 'WARN'}] Own photo library: {n} photos in PHOTO_DIR" if lp.enabled
                  else "[WARN] PHOTO_DIR is set but the folder does not exist")
        from core.trends import fetch_trends
        tr = fetch_trends()
        print(f"[{'OK' if tr else 'WARN'}] Google Trends ({config.TRENDS_GEO}): {len(tr)} trending searches"
              + ("" if tr else " - unavailable, ranking without the trending bonus"))
        if config.REAL_PHOTOS and "openverse" in config.PHOTO_SOURCES:
            from core.openverse import OpenverseClient
            try:
                n = len(OpenverseClient().search("cricket stadium"))
                print(f"[OK] Openverse reachable ({n} results)" + ("" if (config.OPENVERSE_CLIENT_ID and config.OPENVERSE_CLIENT_SECRET)
                                                           else " - anonymous: ~5 requests/hour; see README to register a free client"))
            except Exception as e:  # noqa: BLE001
                print("[WARN] Openverse:", mask_secrets(str(e))[:160])
        import tempfile
        vo = synthesize("This is a quick voice test for the reel.", Path(tempfile.mkdtemp()))
        if vo:
            print(f"[OK] Voiceover works ({vo.duration:.1f}s) via {vo.provider or config.TTS_PROVIDER}")
            if vo.provider == "gemini" and config.TTS_PROVIDER == "auto":
                print("[HINT] Edge voices unavailable (run: python -m pip install -r requirements.txt). Gemini voice is used instead; "
                      "it works, but Edge gives more precise caption timing.")
        else:
            print("[WARN] No voiceover available -> Reels will have captions only. Check TTS_PROVIDER / internet access")
    return 0 if ok else 1


def cmd_preview(args) -> int:
    res = Pipeline().run_cycle(force=True, dry_run=True, fmt=args.format)
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
    res = Pipeline().run_cycle(force=args.force, dry_run=True if args.dry_run else None, fmt=args.format)
    report = res.pop("report", "") if isinstance(res, dict) else ""
    if report:
        print("\n===== EDITORIAL DECISION =====\n" + report + "\n==============================\n")
    log.info("Result: %s", json.dumps(res, ensure_ascii=False))
    print(json.dumps(res, ensure_ascii=False))
    return 1 if res.get("status") == "failed" else 0


def cmd_explain(args) -> int:
    """Print the editorial decision records of the last attempts (why each story got its format and media)."""
    from core.editorial import format_report, read_decisions
    recs = read_decisions(config.DATA_DIR, limit=args.last)
    if not recs:
        print("No decision records yet (data/decisions.jsonl). Run: python main.py run --force --dry-run")
        return 0
    for rec in recs:
        print(format_report(rec) + "\n" + "-" * 70)
    return 0


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
    pv = sub.add_parser("preview")
    pv.add_argument("--format", choices=["photo", "carousel", "reel", "video", "card"], help="build this format (default: the editorial engine decides)")
    r = sub.add_parser("run")
    r.add_argument("--format", choices=["photo", "carousel", "reel", "video", "card"], help="force this format for this run (default: the editorial engine decides)")
    r.add_argument("--force", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    sub.add_parser("daemon")
    sub.add_parser("status")
    sub.add_parser("report")
    ex = sub.add_parser("explain", help="show why the last posts got their format and media (editorial decision records)")
    ex.add_argument("--last", type=int, default=3, help="how many records to show")
    sub.add_parser("models")
    sub.add_parser("clips")
    sub.add_parser("sources", help="test every news feed (built-in + EXTRA_FEEDS)")
    sub.add_parser("fb-check", help="check the Facebook Page token in .env and say what to fix")
    fp = sub.add_parser("fb-pages", help="list your Facebook Pages with Page tokens ready for .env")
    fp.add_argument("--user-token", required=True, help="user token from Graph API Explorer (pages_show_list etc.)")
    fp.add_argument("--app-id", help="Meta App ID (App settings > Basic) for a non-expiring Page token")
    fp.add_argument("--app-secret", help="Meta App Secret (same page); keep it private")
    fp.add_argument("--page-id", help="your Page ID; use when the Pages list comes back empty")
    fp.add_argument("--save", action="store_true", help="write FB_PAGE_ID and the Page token into .env for you (token not printed)")
    rx = sub.add_parser("remix", help="prepare script + caption + checklist for an in-app Remix of an official clip")
    rx.add_argument("--category", choices=["News", "Technology", "Sports", "Entertainment", "Business", "Science"])
    rx.add_argument("--topic", help="keyword filter, e.g. cricket")
    sub.add_parser("trending", help="show Google Trends and how today's news ranks against it")
    ft = sub.add_parser("footage", help="preview which footage would be chosen for a topic")
    ft.add_argument("topic", help="e.g. \"West Bengal bypolls voters tribunal appeals\"")
    ft.add_argument("--category", default="News", choices=["News", "Technology", "Sports", "Entertainment", "Business", "Science"])
    pc = sub.add_parser("post-clip", help="publish a clip you have the rights to as a Reel")
    pc.add_argument("--video", required=True, help="path to your video file")
    pc.add_argument("--topic", required=True, help="one-line description of the story")
    pc.add_argument("--credit", required=True, help="who owns the footage, shown on screen and in the caption")
    pc.add_argument("--confirm-rights", action="store_true", help="you own it or have permission/licence to post it")
    pc.add_argument("--notes", help="extra verified facts for the caption (optional)")
    pc.add_argument("--category", choices=["News", "Technology", "Sports", "Entertainment", "Business", "Science"])
    pc.add_argument("--no-news", action="store_true", help="do not look up matching news (use --notes for facts)")
    pc.add_argument("--dry-run", action="store_true", help="build it into ./preview without publishing")
    args = ap.parse_args(argv)
    return {"verify": cmd_verify, "preview": cmd_preview, "run": cmd_run, "daemon": cmd_daemon,
            "status": cmd_status, "report": cmd_report, "explain": cmd_explain, "models": cmd_models, "clips": cmd_clips, "trending": cmd_trending, "fb-pages": cmd_fb_pages, "sources": cmd_sources, "fb-check": cmd_fb_check, "remix": cmd_remix, "footage": cmd_footage, "post-clip": cmd_post_clip}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
