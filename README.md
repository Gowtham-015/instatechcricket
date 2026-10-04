# Instagram AI News Agent

Fully automated Instagram news page: collects news from RSS feeds, picks the best story for the moment,
writes the copy with Google Gemini, renders a branded **5-slide carousel** (1080×1350 JPEG), and publishes it
through the official **Instagram Graph API**, on a schedule, with safety limits and monitoring.

```
RSS feeds ─► dedupe ─► cluster same story ─► rank + category balance ─► Gemini copy (validated)
        ─► render carousel (Pillow) ─► public host (Cloudinary/GitHub/own) ─► Instagram publish ─► history/insights
```

## What you get

| Area | Behaviour |
|---|---|
| Collection | 15 feeds in 4 categories (News, Technology, Sports, Entertainment), timeouts + retries, one broken feed never stops a run, **a total outage is reported as a failure** |
| Quality | URL/title de-dup, cross-source clustering, "already posted" check (even if another outlet reports the same story), sensitive-topic filter (configurable) |
| Ranking | freshness 30 %, source quality 20 %, importance 20 %, cross-source confirmation 15 %, post suitability 15 % |
| Category balance | picks the category most *under-represented vs `CATEGORY_WEIGHTS`* in the last 24 h, so no category starves |
| Copy | Gemini (`google-genai` SDK, model configurable) returns JSON that is **validated**: story-specific "why it matters", no generic filler, **no invented numbers** (every multi-digit number must appear in the sources). If AI fails twice an extractive fallback writes from the sources and omits what it can't state honestly |
| Visuals | original typographic cards (no third-party news photos, so no copyright/hotlink problems), auto-fit text, emoji-safe |
| Publishing | container, status polling, publish; carousel (2-10), JPEG only, URL reachability check before calling Instagram, quota check via `content_publishing_limit` |
| Safety | single-instance lock, slot bookkeeping, daily cap, min interval, never-blind-retry of writes (lost responses are verified against recent media, so **no duplicate posts**), per-story attempt limit, circuit breaker, throttled webhook alerts, secrets masked in logs |
| Storage | every state file is pruned; only 4 small JSON files are committed; caches/media are never committed |

## 1. Instagram setup (one-time)

1. Instagram account type must be **Professional** (Business or Creator), linked to a **Facebook Page** (for the
   Facebook Login flow).
2. Create an app at <https://developers.facebook.com/> (type *Business*) and add the **Instagram** product.
3. Grant permissions: `instagram_basic`, `instagram_content_publish`, `pages_show_list`,
   `pages_read_engagement` (for your own account the app can stay in development mode with you as admin/tester).
4. Generate a **long-lived access token** and find your **Instagram professional account ID** (`IG_USER_ID`).
5. Using *Instagram API with Instagram Login* instead? Set `IG_API_HOST=https://graph.instagram.com`.

> Meta changes API versions, permissions and limits over time. Always cross-check with the official
> [Content Publishing docs](https://developers.facebook.com/docs/instagram-platform/content-publishing).
> At the time of writing: **JPEG only**, the image must be at a **public URL**, containers expire after 24 h,
> and API-published posts are limited per rolling 24 h window (this agent defaults to **6/day**).
> `IG_API_VERSION` is configurable (default `v26.0`); bump it if Meta retires that version.

## 2. Other keys

* **Gemini**: <https://aistudio.google.com/apikey> -> `GEMINI_API_KEY` (model via `GEMINI_MODEL`).
* **Image hosting** (Instagram downloads your images itself, so they need a public https URL):
  * `MEDIA_HOST=cloudinary` *(recommended, free tier is plenty)* -> `CLOUDINARY_*`
  * `MEDIA_HOST=github` -> public repo only; images go to a separate `media` branch and are deleted after publishing
  * `MEDIA_HOST=local` -> you serve `public/media/` yourself over https and set `MEDIA_BASE_URL`

## 3. Install & test (nothing is published until you say so)

```bash
python -m venv venv && source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # fill it in
python main.py verify     # checks IG account, quota, media-host round trip, Gemini key
python main.py preview    # builds a REAL post from live news into ./preview (carousel + caption.txt)
python main.py run --dry-run
```

Fonts: DejaVu / Liberation / Arial / Segoe are auto-detected. Use `FONT_PATH=/path/Bold.ttf` for your brand font.
Cards are English-only by default (Latin glyphs); supply a Unicode font for other scripts.

## 4. Deploy (pick one)

**GitHub Actions (free, no server)**: push this folder to a repo, add secrets `IG_USER_ID`, `IG_ACCESS_TOKEN`,
`GEMINI_API_KEY`, `CLOUDINARY_*` (optional `ALERT_WEBHOOK_URL`) and variables `BRAND_NAME`, `ACCOUNT_HANDLE`,
`POST_TIMES`, `CATEGORY_WEIGHTS`. The workflow runs **every 30 minutes**; the agent only posts when a slot from
`POST_TIMES` is due (90-minute grace so delayed GitHub runs don't skip posts). State is committed after each run.
Private repos use roughly 1,500 Actions minutes/month at this frequency; public repos are free.

**Docker / VPS**: `docker build -t insta-agent . && docker run -d --env-file .env -v $PWD/data:/app/data insta-agent`
(or `python main.py daemon`; `Procfile` included). Setting `PORT` exposes a tiny `/` health endpoint.

## 5. Commands

| Command | Purpose |
|---|---|
| `python main.py verify` | validate config, IG token/quota, hosting, Gemini |
| `python main.py preview` | build a post without publishing |
| `python main.py run [--force] [--dry-run]` | one cycle (cron). Exit code 1 on failure |
| `python main.py daemon` | scheduler loop (`DAEMON_INTERVAL_MINUTES`, default 15) |
| `python main.py status` | health, posts today, slot state |
| `python main.py report` | per-category performance (reach/interactions) from stored insights |

## 6. Configuration highlights (`.env.example` lists everything)

| Variable | Default | Meaning |
|---|---|---|
| `POST_TIMES` | `09:00,13:00,18:00,21:00` | daily slots in `TIMEZONE` (`Asia/Kolkata`) |
| `MAX_POSTS_PER_DAY` / `MIN_POST_INTERVAL_MINUTES` | 6 / 60 | pacing (quality over volume) |
| `CATEGORY_WEIGHTS` | `News:3,Technology:3,Sports:2,Entertainment:2` | target mix |
| `BREAKING_ENABLED` | true | a story with score >= `BREAKING_SCORE`, 3+ sources, under 3 h old may post outside a slot (still capped) |
| `SKIP_SENSITIVE` | true | skip stories containing terms from the sensitive list in `core/ranker.py` |
| `ALLOW_FALLBACK_POSTS` | true | publish extractive copy when Gemini is down (false = skip instead) |
| `DRY_RUN` | false | never publish |

## 7. Operations

* **Alerts**: set `ALERT_WEBHOOK_URL` (Slack/Discord/any JSON webhook). You get a message on an invalid/expired
  token (immediately) and when the circuit breaker opens (5 consecutive failures, then pauses 3 h).
* **Token expiry**: if Instagram returns an auth error the run fails fast with a clear message and alert. Replace
  `IG_ACCESS_TOKEN` and re-run `python main.py verify`.
* **Logs**: `logs/agent.log` (rotating); tokens/keys are masked everywhere.
* **Copyright & ethics**: cards use original design and the agent's own wording; every post credits its sources in
  the caption and on the last slide. Check local rules on republishing news summaries and label AI usage if your
  region or Instagram's policies require it.

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `hosted image not publicly reachable as JPEG` | host URL private or wrong type; run `verify`; with `github` host the repo must be public |
| `container ... status ERROR` | image rejected (format/size/ratio); cards are 4:5 JPEG, so check your host doesn't re-encode |
| `Instagram token invalid/expired` | regenerate the token; check the permission list in section 1 |
| Always `idle: no slot due` | outside `POST_TIMES` windows; use `run --force` to test |
| Repeated `fallback` mode | Gemini key/model problem, see warnings in the log; fix `GEMINI_MODEL` |

## 9. Project layout

```
main.py                  CLI
core/config.py           validated env config (one name per secret)
core/collector.py        RSS fetch + normalise      core/feeds.py   sources, tiers, category styles
core/dedupe.py           dedupe + clustering        core/ranker.py  scoring + category balance
core/ai_writer.py        Gemini + validation + fallback
core/image_renderer.py   Pillow carousel cards
core/media_host.py       Cloudinary / GitHub / local hosting
core/instagram_client.py Graph API client           core/pipeline.py  one publishing cycle
core/schedule.py  health.py  analytics.py  storage.py
tests/test_agent.py      offline tests (python -m unittest discover -s tests -v)
.github/workflows/instagram_agent.yml   Dockerfile   Procfile
```
