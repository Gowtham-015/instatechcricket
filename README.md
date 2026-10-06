# Instagram AI News Agent

Fully automated Instagram news page: collects news from RSS feeds, picks the best story for the moment,
writes the copy with Google Gemini, renders either a branded **5-slide carousel** (1080x1350 JPEG) or a
**vertical Reel video** (1080x1920 MP4), and publishes it through the official **Instagram Graph API**, on a
schedule, with safety limits and monitoring.

```
RSS feeds ─► dedupe ─► cluster same story ─► rank + category balance ─► Gemini copy (validated)
        ─► render carousel OR Reel video (Pillow + ffmpeg) ─► public host (Cloudinary/GitHub/own) ─► Instagram publish ─► history/insights
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
| Reels (video) | 9:16 MP4 with stock footage, AI-written voiceover, word-synced animated captions, hook headline and outro; text kept inside Instagram's UI-safe zones; H.264 + AAC, ~20-30 s. Mixed with carousels by `CONTENT_FORMATS`. If video can't be built, it posts a carousel instead of failing |
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

## Reels (video)

Reels are built like real Reels, not info cards in a video:

* **moving background footage** matched to the story (royalty-free stock from Pixabay/Pexels, or your own clips),
* a **voiceover** reading a short script written for the ear (not the carousel text),
* big **word-by-word animated captions** that pop in and highlight each word as it is spoken,
* a hook headline in the first seconds, category tag + handle, and a short "follow" outro.

Setup (all optional, every step degrades gracefully):

| Want | Do | If you skip it |
|---|---|---|
| Real footage | one of: free `PIXABAY_API_KEY` (<https://pixabay.com/api/docs/>), `PEXELS_API_KEY` (new keys are sometimes paused), or your own clips in `BROLL_DIR` (below) | animated moving background |
| Voiceover | nothing: `TTS_PROVIDER=auto` uses Edge neural voices (`TTS_VOICE=en-IN-NeerjaNeural`), then Gemini TTS (`GEMINI_TTS_VOICE=Kore`; caption timing is aligned to the real pauses in the audio) | captions at reading speed, silent track |
| Music | `MUSIC_PATH` = a **royalty-free** file you have the rights to; played quietly under the voice (`MUSIC_VOLUME`) | no music |

* `CONTENT_FORMATS=carousel:1,reel:1` alternates image and video posts (`reel:1` = video only, `carousel:2,reel:1` = 2:1).
* `REEL_STYLE=cards` brings back the old "info cards as a slideshow" video.
* Video encoding needs ffmpeg: `pip install -r requirements.txt` installs `imageio-ffmpeg`, which bundles it.
  If video can't be built for any reason the agent posts a carousel instead of failing.
* Footage sources are tried in this order: Pexels -> Pixabay -> your `BROLL_DIR` -> animated background
  (`STOCK_PROVIDER=auto`; force one with `pexels|pixabay|local|none`). A provider outage never fails a post.
* **Footage relevance:** search terms come from the story's topic first (concrete scenes such as "voting ballot box",
  "courtroom gavel", "cricket match stadium"), then the AI's suggestions, then category defaults; abstract words
  (issues, appeals, pending ...) are never searched. A stock clip is used only if its tags/title match the search
  term. Irrelevant footage is never used: no match means your own clips, then an animated background.
  Check any topic with `python main.py footage "keywords"`.
* Rendering takes about 1-2 minutes per Reel on a normal PC (faster on multi-core).
* Footage is *illustrative b-roll* from generic search terms; it is never presented as footage of the real event.
  Pexels and Pixabay licences: free for commercial use, attribution optional (a small "Footage: Pexels/Pixabay"
  credit is added to the caption; turn off with `FOOTAGE_CREDIT=false`). Pixabay landscape clips are centre-cropped to 9:16
  (portrait clips look sharper; Pixabay's API cannot filter by orientation). The Edge voice service is an unofficial endpoint of the Edge
  browser's read-aloud feature and may change; Gemini TTS is the official fallback.
* Instagram's own music library is **not** available through the API, so audio must be inside the video file.
* Test one without publishing: `python main.py preview --format reel` -> `preview/*.mp4` + caption.
  Publish one right now: `python main.py run --force --format reel`.

### Footage without any API key (your own clips)

1. Create a folder, e.g. `C:\Users\you\broll`, with sub-folders `News`, `Technology`, `Sports`, `Entertainment`.
2. Download free **vertical** clips (10-30 s, HD) from sites whose licence allows commercial use, e.g.
   pexels.com/videos, pixabay.com/videos, mixkit.co/free-stock-video, coverr.co. Check each site's licence and avoid
   clips with logos, readable brand names or identifiable people in sensitive contexts.
3. Put 6+ clips per category in the matching sub-folder (loose clips in the main folder are used for any category).
4. Set `BROLL_DIR=C:\Users\you\broll` in `.env` and check with `python main.py clips`.

The agent prefers clips whose **file or folder name matches the story** (name them like the scene: `election_voting_queue.mp4`,
`courtroom_gavel.mp4`, `cricket_stadium_crowd.mp4`), then the category folder, so Reels stay relevant and varied. Add new clips from time to time.

## Posting real / official footage (your clip, your rights)

Auto-generated Reels use illustrative stock footage. For a news event where the *actual* footage matters (e.g. a team's
medal celebration), publish the real clip yourself through the agent:

```powershell
python main.py post-clip --video "C:\clips\celebration.mp4" `
  --topic "India's cricket teams win Asian Games gold" `
  --credit "BCCI / Star Sports" --confirm-rights --dry-run      # preview first, then remove --dry-run
```

The agent reformats the clip to 9:16 with a blurred fill (nothing is cropped away), **keeps the original audio**
(crowd, commentary), adds a hook headline and an on-screen `Video: <credit>` line, finds the matching real news story
for a fact-grounded caption (`--no-news` + `--notes "verified facts"` to skip the lookup), and publishes it. The story is
recorded in history so scheduled posts will not repeat it.

**Rights are yours to secure.** The agent never downloads other people's videos. Broadcaster, federation and
league footage is copyrighted: use it only with a licence or written permission (news agencies such as ANI, PTI, Reuters,
AP and Getty license video; many organisations also grant permission on request), or footage you or your team filmed.
Without that, Instagram can remove the post and issue copyright strikes against your account. Instagram's in-app
**Remix** / **Collab** features are the platform-sanctioned way to build on someone else's Reel; they cannot be
automated through the API.

For a voice-free, more natural look on automatic Reels set `TTS_PROVIDER=none` (captions + footage + optional music).

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
| `python main.py preview [--format reel]` | build a post (carousel or Reel) without publishing |
| `python main.py run [--force] [--dry-run] [--format reel]` | one cycle (cron). Exit code 1 on failure |
| `python main.py daemon` | scheduler loop (`DAEMON_INTERVAL_MINUTES`, default 15) |
| `python main.py status` | health, posts today, slot state |
| `python main.py post-clip --video f.mp4 --topic "..." --credit "Owner" --confirm-rights [--dry-run]` | publish a clip you have the rights to as a Reel |
| `python main.py footage "keywords" [--category Sports]` | preview which footage terms/clips would be used for a topic |
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
| `CONTENT_FORMATS` | `carousel:1,reel:1` | mix of image carousels and Reels |
| `REEL_STYLE` | `dynamic` | `dynamic` = footage + voice + captions, `cards` = old slideshow |
| `PEXELS_API_KEY` / `TTS_VOICE` / `MUSIC_PATH` | empty / `en-IN-NeerjaNeural` / empty | footage, narrator voice, optional royalty-free music |
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
| `No module named 'edge_tts'` | `python -m pip install -r requirements.txt` (Gemini voice is used meanwhile; Edge gives more precise caption timing) |
| `Pexels key rejected (HTTP 401)` | the key in `.env` is a placeholder or wrong: delete the `PEXELS_API_KEY` line or paste a real key. Placeholders like `your_key` are ignored automatically |
| `run --dry-run` says `idle ... (breaking-only)` | normal outside your `POST_TIMES` windows; use `run --force --dry-run` to test any time |
| Reel has no voice | `verify` shows the TTS result; check internet access or set `TTS_PROVIDER=gemini`; captions still work |
| Footage doesn't match the story | run `python main.py footage "your story keywords"` to see the search terms and which clips pass the relevance check. Only clips whose tags/title match a search term are used (otherwise: your `BROLL_DIR`, then an animated background). For recurring topics, add your own clips named after the topic, e.g. `election_voting_queue.mp4`, `courtroom_gavel.mp4` |
| Reel background is a plain moving gradient | no footage source: add `PIXABAY_API_KEY` or fill `BROLL_DIR` (see above) |
| `Reels disabled (ffmpeg not available)` | `python -m pip install imageio-ffmpeg`; `verify` then shows `[OK] ffmpeg ready` |
| Reel container `status ERROR` | Instagram's reason is printed in the error; check the video is H.264+AAC (the built-in encoder is) and the host serves it publicly |
| Repeated `fallback` mode | Gemini key/model problem, see warnings in the log; fix `GEMINI_MODEL` |

## 9. Project layout

```
main.py                  CLI
core/config.py           validated env config (one name per secret)
core/collector.py        RSS fetch + normalise      core/feeds.py   sources, tiers, category styles
core/dedupe.py           dedupe + clustering        core/ranker.py  scoring + category balance
core/ai_writer.py        Gemini + validation + fallback
core/image_renderer.py   Pillow cards (feed 4:5 and Reel 9:16 layouts)
core/reel_studio.py      Reel orchestrator (voice + footage + render)
core/clip_post.py        publish your own licensed clip as a Reel
core/reel_renderer.py    footage background, animated captions, outro
core/tts.py              voiceover + word timings    core/stock.py  Pexels / Pixabay / own clips
core/video_renderer.py   ffmpeg helpers + legacy card-slideshow video
core/media_host.py       Cloudinary / GitHub / local hosting
core/instagram_client.py Graph API client           core/pipeline.py  one publishing cycle
core/schedule.py  health.py  analytics.py  storage.py
tests/test_agent.py      offline tests (python -m unittest discover -s tests -v)
.github/workflows/instagram_agent.yml   Dockerfile   Procfile
```
