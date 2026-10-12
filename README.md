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
| Collection | 26 configurable feeds in 6 categories (see *Editorial newsroom*), timeouts + retries, one broken feed never stops a run, **a total outage is reported as a failure** |
| Quality | URL/title de-dup, cross-source clustering, "already posted" check (even if another outlet reports the same story), sensitive-topic filter (configurable) |
| Ranking | freshness 30 %, source quality 20 %, importance 20 %, cross-source confirmation 15 %, post suitability 15 % |
| Category balance | picks the category most *under-represented vs `CATEGORY_WEIGHTS`* in the last 24 h, so no category starves |
| Copy | Gemini (`google-genai` SDK, model configurable) returns JSON that is **validated**: story-specific "why it matters", no generic filler, **no invented numbers** (every multi-digit number must appear in the sources). If AI fails twice an extractive fallback writes from the sources and omits what it can't state honestly |
| Visuals | original typographic cards (no third-party news photos, so no copyright/hotlink problems), auto-fit text, emoji-safe |
| Reels (video) | 9:16 MP4 with stock footage, AI-written voiceover, word-synced animated captions, hook headline and outro; text kept inside Instagram's UI-safe zones; H.264 + AAC, ~20-30 s. Only chosen when the editorial engine decides narration helps (or `FORMAT_POLICY=legacy`). If video can't be built or fails Instagram's checks, it falls back to a safer format |
| Publishing | container, status polling, publish; carousel (2-10), JPEG only, URL reachability check before calling Instagram, quota check via `content_publishing_limit` |
| Safety | single-instance lock, slot bookkeeping, daily cap, min interval, never-blind-retry of writes (lost responses are verified against recent media, so **no duplicate posts**), per-story attempt limit, circuit breaker, throttled webhook alerts, secrets masked in logs |
| Storage | every state file is pruned; only small state JSON files (history, schedule, attempts, health, pending, decisions, style) are committed; caches/media are never committed |

## Editorial newsroom: the story and the media decide the format (v2)

Earlier versions alternated carousels and Reels by weight (`CONTENT_FORMATS`), so every post became a template with
new text. With `FORMAT_POLICY=editorial` (the default) a decision engine looks at the **story** and at the **media it
may legally and honestly use**, then picks the format. `CONTENT_FORMATS` is ignored in this mode (set
`FORMAT_POLICY=legacy` to get the old behaviour back).

| Priority | Rule | Result |
|---|---|---|
| H | Thin sourcing, conflicting figures or low confidence (sensitive topics need `MIN_CORROBORATION_SENSITIVE` outlets) | **hold**: nothing is published, the reason is logged |
| V | An authorised video that really shows this event exists | **original video** (original sound kept, no AI voice) |
| C | Several distinct relevant photos AND 3+ separate facts, not a simple update | **photo carousel** |
| R | Narration clearly helps (`REEL_NARRATION_MIN`) AND real photos exist (or labelled illustrative footage for tech/science/business) | **Reel**, voice only when it helps |
| P | A relevant real photo exists and narration adds little | **single photo post** (photo is the focus) |
| F | Nothing authentic found | **clean fact card** (an editorial graphic, never a fake photo or generic Reel) |

If a file will not render or Instagram would refuse it (checked *before* upload: codec, fps, duration, size, aspect), the
engine falls back to the next format and records the format that was **actually** published.

**Where media comes from** (`MEDIA_SOURCE_ORDER`, default `official,local,commons,openverse`)

| Source | What it supplies | Licence rule | Needs |
|---|---|---|---|
| `official` | media linked from the news items, **only** from `OFFICIAL_MEDIA_DOMAINS` (default NASA) | domain you declared as reusable | nothing |
| `local` | your own `PHOTO_DIR` photos and `VIDEO_DIR` videos | you own it / have permission. A video needs `<name>.mp4.rights.json` | your files |
| `commons` | Wikimedia Commons photos (and `COMMONS_VIDEO` videos) | CC0, Public Domain, CC BY, GODL-India only. NC, ND, share-alike are refused | nothing |
| `openverse` | open-licensed Flickr/NASA/Smithsonian photos | same open-licence list | optional `OPENVERSE_CLIENT_ID/SECRET` |

Not supported, on purpose: downloading Google Images, YouTube, X, BCCI/ICC/Cricbuzz or article photos. Public
availability is not permission. Use `post-clip` or `VIDEO_DIR` for footage you have rights to.
Stock footage (Pexels/Pixabay) is only ever labelled *illustrative*, never "event footage".

**How a photo is accepted.** Queries come from the story's own people, teams, places and products, not the category.
Each candidate is checked **before download** (documented licence, name in its own title/description, wrong person, wrong
match "A vs B", wrong place/tone/year), then after download (decodes, `MEDIA_MIN_WIDTH/HEIGHT`, duplicate-picture
detection). Confidence must reach `MEDIA_MIN_CONFIDENCE`. A photo of the right subject but not shown to be this event is
published only as a labelled **FILE PHOTO** (`ALLOW_FILE_PHOTOS`). Every accepted asset keeps a provenance record
(original URL, creator, licence, attribution, story, retrieval time, confidence, rationale).

**Authorised video rights file** (`VIDEO_DIR/match.mp4.rights.json`):

```json
{"title": "India vs X final: trophy presentation", "description": "trophy ceremony, India, final",
 "permission": "official_permitted", "creator": "Board name", "licence": "written permission 2026-10-01",
 "source_url": "https://...", "date": "2026-10-11", "depicts": "event", "keep_audio": true}
```

**News sources and trends.** 26 built-in feeds (the original 15 plus PIB, Al Jazeera, BBC World, Indian Express,
Hindustan Times, Economic Times Markets, BusinessLine, BBC Business, NASA, BBC Science, ...). Add or switch off sources
without code via `SOURCES_FILE` (JSON `{"add":[{"category","name","url","quality"}],"disable":["Al Jazeera"],"quality":{}}`),
`EXTRA_FEEDS` or `DISABLED_SOURCES`; `python main.py sources` reports the coverage that actually answered. Trend signals
(`TREND_PROVIDERS=google,wikipedia`) are Google Trends' public RSS and Wikimedia's daily pageviews. If a provider is down
it is reported as *unavailable*, never as "nothing trending", and ranking continues on freshness, source quality and
cross-source corroboration. No search volume or view count is ever invented. Significant stories (`SIGNIFICANT_SCORE`, 3+
outlets, fresh) are not held back by category balancing.

**Audit trail.** Every attempt writes a record to `data/decisions.jsonl` (story, sources, trend signals, corroboration,
media accepted/rejected with reasons, licence result, rule trail, chosen format and why, final media type, outcome).
Read it with `python main.py explain --last 5`. Example line:
`Selected single-photo post: verified event photograph available; original video unavailable; narration unnecessary.`

**Dry run (publishes nothing, changes no state)**

```bash
python main.py run --force --dry-run                    # the engine decides
python main.py run --force --dry-run --format photo     # try photo | carousel | reel | video | card
python main.py explain --last 1
```

Output goes to `./preview/` (image/video, caption, decision report). On GitHub: *Actions > Instagram AI News Agent >
Run workflow*, tick **dry_run**, then download the `dry-run-preview` artifact from the run.

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

* **real documentary photographs** of the story's people, places and institutions (Wikimedia Commons, free licences,
  slow Ken-Burns pan, credited on screen and in the caption) mixed with **relevant stock footage** (Pixabay/Pexels or your own clips),
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
* **Real photos (`REAL_PHOTOS=true`, no key needed):** the story's named people, places and institutions are searched on
  Wikimedia Commons ("West Bengal", "Election Commission of India", "Eden Gardens" ...). Only **CC0 / Public Domain / CC BY**
  photos are used (never share-alike, non-commercial, no-derivatives, non-free, logos, flags, maps), only if the title/
  description/categories actually match the name, and each photo's author + licence is shown on screen and listed in the
  caption (`📷 Images: ...`), which CC BY requires: do not remove it. **Limits:** Commons has photos of the people and
  places involved, not of a breaking event that happened hours ago; for that, use `post-clip` with footage you are licensed
  to use. If no suitable photo exists, the Reel falls back to relevant stock footage, your clips, then an animated background.
* **Preview == published:** footage/photo choices are seeded by the story, so the same story always gets the same
  visuals; the preview you approve is what gets posted.
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

## Trending news and photo accuracy

**What counts as "trending":** each story is scored on freshness, source quality, importance and how many outlets
report it, plus a bonus when it matches **Google Trends' "trending now" searches** for your country
(`TRENDS_GEO=IN`, public RSS feed). A story must be covered by at least `MIN_SOURCES=2` outlets **or** match a Google
trend. Roundups, live blogs, newsletters and "top stories" pages are never used, and the AI is told to refuse
scripts built from several unrelated stories. See it in action: `python main.py trending`.

**Google Images / YouTube:** Google's image search API is closed to new users (shutting down in 2027) and its
"usage rights" filter is not reliable, so the agent does not use it. Instead it searches **Wikimedia Commons** and
**Openverse** (an open-licence search engine covering Flickr, NASA, Smithsonian ...), which return each photo's
licence and author. Only CC0 / Public Domain / CC BY are used; every credit appears on screen and in the caption.
Downloading YouTube videos is not supported (it violates YouTube's terms and "Creative Commons" labels on re-uploaded
news are often wrong); use `post-clip` with footage you are licensed to use.

**Wrong-person protection:** for people's names (and any name that is not clearly a place or institution), a photo
is used only if its title/description/tags also mention the story's topic. A hospital doctor named like a fashion
designer will not be used for a fashion story.

**Openverse limits:** anonymous use is about 5 requests/hour (results are cached 24 h). For regular use register a free
client: `curl -X POST https://api.openverse.org/v1/auth_tokens/register/ -H "Content-Type: application/json" -d
"{\"name\":\"my-news-agent\",\"description\":\"Instagram news reels\",\"email\":\"you@example.com\"}"`,
confirm the e-mail, and put the returned values in `OPENVERSE_CLIENT_ID` / `OPENVERSE_CLIENT_SECRET`.

## Cricket (and other official-footage topics): what is allowed, what works

BCCI, ICC and broadcasters publish their content for viewing, not for re-use: their terms prohibit copying images and
video without permission, and Instagram can mute, remove or strike unlicensed re-uploads. Three legitimate routes are built in:

1. **Result cards (automatic):** for any story reporting a cricket match result, the Reel opens with a broadcast-style
   result card (teams, scores, result, top performer). Scores are facts and the card is the agent's own graphic; every
   number must appear in the source articles or the card is dropped (the Reel is still made). Photos of the players and
   stadium come from Wikimedia Commons / Openverse (free licences, credited).
2. **Remix helper (one minute in the app):** `python main.py remix --topic cricket` picks the best trending story and
   writes a ready package to `drafts/`: sources, on-screen headline, a script to read, a paste-ready caption with
   hashtags and a checklist. You then open the official Reel on Instagram and use **Remix** (credit is automatic; it only
   works where the owner allows it). Never download and re-upload the clip.
3. **Ask permission once:** email the rights contact (BCCI lists website@bcci.tv for rights clearance) for credited
   news use of short clips/photos; franchises and state associations often agree. `post-clip` publishes any footage
   you have written permission or a licence for.

## Every Reel looks and sounds different

With `REEL_VARIETY=true` (default) each Reel gets its own look, so followers do not feel they are watching the same Reel again:

| What changes | Options |
|---|---|
| Voice | rotates through `TTS_VOICES` (default: Neerja and Prabhat), plus small speed and pitch changes |
| Word highlight colour | category colour, yellow, cyan, lime, pink, orange |
| Captions | 2 or 3 words at a time, normal or UPPERCASE, three sizes, three heights |
| Headline | left, centred, or on a dark panel with an accent bar; three heights; fade or rise-in |
| Footage colour | natural, vivid, cool, warm, punchy, vignette (text is never graded) |
| Photo movement | pan direction flips |
| Closing card | four different call-to-action lines |
| Script | the voiceover opens in a different way (surprising fact, question, number, what it means for you, short statement) |

Rules: two Reels in a row never share the voice, caption colour, headline layout or caption height, and the colour and
closing line also differ from the Reel before that. The same story always gets the same look, so a preview matches what is
published. Recent looks are stored in `data/style_history.json`. To keep one voice, set `TTS_VOICES` to a single voice;
to switch everything off, set `REEL_VARIETY=false`. The Reel history (`data/history.json`) records the look used.

## Facebook Page (same content, optional)

Add the Facebook Page connected to your Instagram and every post goes to both. Facebook, unlike Instagram, supports
**link posts**: headline + article link, and Facebook builds the preview card with the publisher's own picture, source name
and click-through, exactly like a Telegram link preview. Modes (`FB_MODE`):

| Mode | What the Page gets |
|---|---|
| `crosspost` (default) | the same Reel (as a Facebook Reel) or the same carousel photos, same caption |
| `link` | a Telegram-style post: headline, why it matters, source + the article link (Facebook adds the official preview picture) |
| `both` | the crosspost **and** the link post as two posts |
| `off` | Instagram only |

**Setup (about 10 minutes):** Facebook needs a **Page token from Facebook Login**; your Instagram Login token cannot post to
a Page.
1. In your Meta app dashboard add the use case **Manage everything on your Page** (permissions `pages_show_list`,
   `pages_manage_posts`, `pages_read_engagement`, `publish_video`; older apps: add *Facebook Login*). You must be an admin of the Page and of the
   app (or a tester); no app review is needed for your own Page.
2. Open **Graph API Explorer**, pick your app, **Get User Access Token**, tick those four permissions (`publish_video` is what allows Reels and videos), generate it.
3. Copy your **App ID** and **App Secret** from the dashboard (*App settings > Basic*), then run
   `python main.py fb-pages --user-token <token> --app-id <id> --app-secret <secret>` (add `--page-id <your Page ID>` if it says "No Pages found", which happens for Pages owned through a Business Portfolio). It swaps your token for a
   long-lived one and prints `FB_PAGE_ID=` and `FB_PAGE_ACCESS_TOKEN=` for every Page you manage, with
   `Page token expires: never`. Paste one Page's two lines into `.env`, set `FB_MODE`, then run `python main.py verify`.
   Easier and safer: add `--save` and the command writes the Page ID and Page token into `.env` for you without showing the
   token (this also avoids pasting the wrong token). Then run `python main.py fb-check`.
   Without `--app-id/--app-secret` it still works, but the Page token may expire and you get a warning.

Useful links:
- Meta apps dashboard: <https://developers.facebook.com/apps/>
- Graph API Explorer (generate the user token): <https://developers.facebook.com/tools/explorer/>
- Access Token Debugger (check expiry and permissions): <https://developers.facebook.com/tools/debug/accesstoken/>
- Pages API getting started: <https://developers.facebook.com/docs/pages/getting-started>
- Page access tokens: <https://developers.facebook.com/docs/pages/access-tokens/>
- Posting to a Page: <https://developers.facebook.com/documentation/pages-api/posts>
- Publishing Reels: <https://developers.facebook.com/documentation/video-api/guides/reels-publishing>

Notes: Facebook Reels via API are limited to 30 per 24 h; the video host must not block Facebook by robots.txt (Cloudinary is
fine); Facebook fetches your hosted media during the post, so it is deleted only afterwards; a Facebook error never fails
or repeats the Instagram post (it is logged and saved in `data/history.json`); link posts only show a picture if the
publisher's page has Open Graph tags and allows Facebook's crawler. Link posts reuse the publisher's own preview card from
their page, not a copy of their photo.

## Why not copy the article photos like the Telegram bot does?

On Telegram the post carries a **link**; Telegram shows the publisher's own preview photo from the publisher's server,
with the source name and "Read full story". Instagram has no link previews: every picture must be uploaded and
hosted as **your own post**, which is a re-upload of the photographer's work (and Instagram matches uploads against
rights holders' content automatically). So the Instagram agent uses photos it is licensed to use instead:

* **Official Government of India photos** (Prime Minister's Office, PIB, MEA) published on Wikimedia Commons under
  **GODL-India**, which allows commercial reuse with attribution; the agent adds the required credit + source URL to the caption.
* **Free-licence photos** (CC0 / Public Domain / CC BY) from Wikimedia Commons and Openverse (Flickr, NASA, Smithsonian).
* **Your own library** (`PHOTO_DIR`): photos you took, bought, or have written permission for, e.g. photos from an
  agency subscription or a press kit. Name them after what they show and put the credit after a double underscore:
  `narendra_modi_eknath_shinde_meeting__PIB.jpg`. Files whose name matches the story's names are used first.

Photos are matched strictly: people need a name match **and** story context (a same-name surgeon never matches a fashion
story); countries/states/cities need two context matches (a generic Tokyo street never illustrates a politics story);
institutions and landmarks (Election Commission of India, Eden Gardens) match by name. CC0 / Public Domain photos need no
on-screen credit; CC BY and GODL-India credits are shown on screen and in the caption.

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
| `python main.py preview [--format photo\|carousel\|reel\|video\|card]` | build a post without publishing |
| `python main.py explain [--last N]` | show why recent posts got their format and media (editorial decision records) |
| `python main.py run [--force] [--dry-run] [--format reel]` | one cycle (cron). Exit code 1 on failure |
| `python main.py daemon` | scheduler loop (`DAEMON_INTERVAL_MINUTES`, default 15) |
| `python main.py status` | health, posts today, slot state |
| `python main.py post-clip --video f.mp4 --topic "..." --credit "Owner" --confirm-rights [--dry-run]` | publish a clip you have the rights to as a Reel |
| `python main.py footage "keywords" [--category Sports]` | preview which footage terms/clips would be used for a topic |
| `python main.py trending` | Google Trends + how today's news ranks against it |
| `python main.py remix [--topic cricket] [--category Sports]` | script + caption + checklist for an in-app Remix of an official clip |
| `python main.py sources` | test every news feed (built-in + `EXTRA_FEEDS`) and show how many fresh items each returns |
| `python main.py fb-check` | check the Facebook Page token in `.env` and say in plain words what to fix (also runs inside `verify`) |
| `python main.py fb-pages --user-token ... --app-id ... --app-secret ...` | list your Facebook Pages + Page tokens to paste into `.env` |
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
| `FORMAT_POLICY` | `editorial` | the engine picks the format per story; `legacy` uses `CONTENT_FORMATS` (`carousel:1,reel:1`) |
| `MEDIA_SOURCE_ORDER` / `MEDIA_MIN_CONFIDENCE` | `official,local,commons,openverse` / `0.6` | where photos/video may come from, and how sure a match must be |
| `REEL_NARRATION_MIN` / `REEL_VOICEOVER` | `0.6` / `auto` | how much a story must gain from narration to be a Reel; voice auto/always/never |
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
| Preview and published background differ | fixed: choices are now seeded by the story (update to the latest version). The voice can still differ slightly between runs |
| No real photos appear | Commons had no licence-safe photo matching the names (normal for very new events); check the log lines `commons: no licence-safe relevant photo for ...`; stock footage is used instead |
| `idle: no suitable story` for hours | few stories pass `MIN_SOURCES=2` and the trend rule; run `python main.py trending` to see why, or set `MIN_SOURCES=1` |
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
core/commons.py          real photos from Wikimedia Commons (licence-checked, credited, same-name safe)
core/openverse.py        open-licensed photos from Flickr/NASA/Smithsonian via Openverse
core/localphotos.py      your own licensed/permitted photo library (PHOTO_DIR)
core/facebook_client.py  Facebook Page posting (link post, photos, Reels)
core/trends.py           Google Trends 'trending now' signal
core/drafts.py           Remix helper (script, caption, checklist for an official clip)
core/reel_renderer.py    footage background, animated captions, outro
core/tts.py              voiceover + word timings    core/stock.py  Pexels / Pixabay / own clips
core/video_renderer.py   ffmpeg helpers + legacy card-slideshow video
core/media_host.py       Cloudinary / GitHub / local hosting
core/instagram_client.py Graph API client           core/pipeline.py  one publishing cycle
core/editorial.py       format decision engine + decision records     core/story.py  story analysis, confidence, corroboration
core/media_match.py     story-to-media retrieval, relevance + licence checks   core/provenance.py  media provenance records
core/photo_post.py      photo / photo-carousel / fact-card renderer    core/video_post.py  authorised-video -> Reel file
core/publishable.py     Instagram API compatibility checks (before upload)
core/schedule.py  health.py  analytics.py  storage.py
tests/test_agent.py, tests/test_editorial.py   offline tests (python -m pytest -q  or  python -m unittest discover -s tests -v)
.github/workflows/instagram_agent.yml   Dockerfile   Procfile
```
