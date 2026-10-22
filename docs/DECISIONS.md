# DECISIONS

Format: **Karar / Gerekçe / Alternatif**. Decisions taken unattended overnight (2026-10-19).

## D-001 Working directory is a non-git copy
- **Karar:** Work in `C:\Users\Emre\Desktop\utube - Kopya` (the session's working dir). It was
  not a git repo, so a fresh repo was initialised with a *baseline* commit of the existing
  state (secrets/media excluded via `.gitignore`), then branch `instagram-migration`.
- **Gerekçe:** Instructions require a branch + per-phase commits; there was nothing to branch from.
- **Alternatif:** Locate the original public repo elsewhere — not visible from this session.

## D-002 git via dulwich
- **Karar:** System git is not installed. Used `dulwich` (pure-Python) through
  `scripts/dev_git.py` (status/commit/branch/log; no push by design).
- **Gerekçe:** Installing system software touches Windows; dulwich is a contained pip package.
- **Alternatif:** winget/portable MinGit install.

## D-003 Repo state differs from the task description
- **Karar:** The copy is already half-migrated to IG: `render_raw_ffmpeg` → RIFE ×3 (72fps) →
  `burn_overlays_ffmpeg`; title card / panels / subscribe / outro already gone; there is an
  unofficial `instagrapi` + ADB-tethering publisher (`publish_on_connect.py`,
  `daily_production.py`, `ig_dashboard.py`). `youtube_analytics.py`, `fetch_channel_stats.py`,
  `platform_config.py`, `requirements.txt`, `ffmpeg_renderer_p2.py` do not exist here.
  I applied the task's *goals* to the code that actually exists.
- **Gerekçe:** Task says decisions are final; the target end-state is what matters.
- **Alternatif:** none sensible.

## D-004 Panel login disabled until a password hash exists
- **Karar:** `scripts/init_secrets.py` generated FERNET_KEY + PANEL_SECRET_KEY into `.env` and
  imported SMTP values from `email_config.json`, but left `PANEL_PASSWORD_HASH` empty. With
  no hash the panel refuses every login. Emre sets it with `--set-panel-password`.
- **Gerekçe:** A generated password would have to be written somewhere readable (docs are
  committed to a public repo). Fail-closed is safer.
- **Alternatif:** random password printed to a gitignored file.

## D-005 Render stages and where text is burned
- **Karar:** `render_base` (30 fps, bg+polish+audio, loudnorm) → RIFE ×2 (`--fps 60`) →
  `finalize_reel` (captions + credit burned on the 60 fps frames, final 8 Mbps encode,
  +faststart). Credit and captions are burned *after* RIFE.
- **Gerekçe:** Interpolating frames that contain changing caption words produces ghosting; text
  burned on the final frames stays crisp. Transcription was moved out of the renderer so the
  pipeline can batch Whisper/Qwen/render separately (Phase 3).
- **Alternatif:** burn everything at 30 fps before RIFE (task text) — visibly worse captions.

## D-006 RIFE failure → frame-duplication fallback, flagged
- **Karar:** If RIFE fails, the 30 fps base is converted to 60 fps by frame duplication; the
  result still passes the format QC but `rife_used=false` is stored in the QC report.
- **Gerekçe:** Unattended production must not stall on one flaky step; the defect is visible.
- **Alternatif:** abort the clip (old behaviour).

## D-007 Background no longer stretched
- **Karar:** The blurred background is now `scale(increase)+crop` instead of a hard 540×960
  stretch.
- **Gerekçe:** The old filter squashed 16:9 footage before blurring; same look, no distortion.

## D-008 legacy_runner.py archived, split into modules
- **Karar:** `downloader.py`, `transcriber.py` (`WhisperSession`, load once per batch),
  `media_utils.py`. The whole monolith (MoviePy, Kokoro, transformers/AWQ) moved to
  `legacy_youtube/legacy_runner.py`. Non-YouTube dead code (instagrapi publisher, its panel,
  ESRGAN) moved to `legacy_misc/`.
- **Gerekçe:** Task allows splitting; nothing active imports the old file (grep-verified).

## D-009 bg_music
- **Karar:** merged `tech__ai_riskli`→`tech_ai_riskli`, `food__cooking_riskli`→`food_cooking_riskli`,
  renamed `automative_riskli`→`automotive_riskli`. New `sectors.music_slug` column (migration 003)
  maps a sector to its folder; lookup = music_slug → slug(name) → `default/` → no music.
  The only DB sector (`IG_deneme31316969`) matches no folder and `default/` is empty → no music.
- **Risk flagged in MORNING_REPORT:** the tracks look like YouTube Audio Library music, whose
  licence covers YouTube videos; Instagram may mute or block them.

## D-011 Ollama / Qwen3-32B — BLOKE, code + mocks only
- **Karar:** Ollama is not installed and `C:\models\Qwen3-32B-Q4_K_M.gguf` does not exist on
  this PC (only the old HF cache `Qwen2.5-14B-Instruct-AWQ`). Wrote `ollama/Modelfile`,
  `llm_client.py`, and `llm_mock.py` (`LLM_BACKEND=mock`). All tests and the E2E dry run use
  the mock. Nothing was downloaded.
- **Not verified:** that Ollama honours `think:false` with this custom TEMPLATE. Mitigation: the
  client also appends `/no_think` and strips `<think>…</think>` before parsing.
- **VRAM:** 19.8 GB weights + ~4 GB KV cache at 16k ctx ≈ 24 GB = both 3060s full. Recommend
  `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0`; if Ollama still offloads to CPU,
  drop `OLLAMA_NUM_CTX` to 8192 (no prompt needs more than ~6k tokens).

## D-012 GPU lock granularity
- **Karar:** One global cross-process file lock (`GPU_LOCK_PATH`). Whisper and Qwen stages hold
  it for the whole stage (model stays loaded); render+RIFE take it per clip, so the comment
  responder / fast lane can slip in between clips.
- **Gerekçe:** Qwen 32B needs both GPUs, so per-GPU locks would not allow parallelism anyway.

## D-013 Captions are assembled by code
- **Karar:** Qwen returns only `{comment, hashtags}`; code appends the hashtags and the
  `🎥 @handle` credit line, and rejects links, @mentions, follow/subscribe CTAs, >2 sentences,
  generic tags (#shorts/#fyp/#viral) and captions ≥0.75 similar to the last 50.
- **Gerekçe:** The credit must always be the original source; the model never writes it.

## D-014 Whisper once per segment, word timestamps reused
- **Karar:** Whisper runs once per 3-min segment with `word_timestamps=True`; the segment text
  feeds clip scoring and the same word list is sliced for each clip's captions. Language is
  auto-detected; non-English sources are skipped.
- **Gerekçe:** Required by the strict batch order (Whisper stage fully before Qwen stage).

## D-015 "Daily" caps are rolling 24 h
- **Karar:** Post caps, reply/day and hide/day caps count the last 24 h, not calendar days.
- **Gerekçe:** The posting window (15:00–05:00 Warsaw) crosses midnight; a calendar day would allow
  2 posts within a few hours around 00:00.

## D-016 Account status transitions
- **Karar:** `WARMUP → ACTIVE` happens automatically in code once a token exists and
  `ig_setup_completed_at + 2 days` has passed (this is the warm-up gate, not an un-pause).
  Qwen's `account_pause` sets `PAUSED_AUTO`; nothing in code (and no Qwen action) can move a
  paused account back to ACTIVE — only the panel's "Devam" button.
- **Karar:** Production only fills inventory for accounts where Emre entered
  `ig_setup_completed_at` (ACTIVE or WARMUP).

## D-017 Perceptual hash = 4 source frames, all must match
- **Karar:** pHash of 4 frames (20/40/60/80 %) of the *source* clip; a duplicate only if all four
  are within 6 bits. Plus URL + time-range overlap (±2 s). Checked before render, after render,
  and again at publish time across accounts.
- **Gerekçe:** Single-frame hashes flag every clip of a podcast as identical (same studio shot).

## D-018 Legacy pool → sources as TRIAL
- **Karar:** Migration 004 copied the hand-picked pool (`https://www.youtube.com/@MrBeast`) into
  `sources` with status TRIAL; it is evaluated by the normal 72 h trial like any discovered source.

## D-019 SQLite in WAL mode
- **Karar:** Five processes share `automation.db`; engine uses WAL + 30 s busy timeout.

## D-020 Instagram API — verified against Meta docs (2026-10-19, web)
Sources: developers.facebook.com → instagram-platform/content-publishing, ig-user/media, ig-container,
ig-user/media_publish, ig-user/content_publishing_limit, instagram-media, instagram-media/insights,
instagram-user/insights, business-login, refresh_access_token, access_token, comment-moderation;
graph-api/changelog (v26.0 released 2026-07-29).

| Item | Verified result | Used in code |
|---|---|---|
| API version | Graph latest **v26.0**; Instagram docs examples still show v25.0 | pinned `IG_API_VERSION=v25.0` (the version the IG docs were checked against); bump to v26.0 after a manual test |
| Host | Instagram Login → `graph.instagram.com` | ✔ |
| Reels flow | `POST /{ig-user-id}/media` (`media_type=REELS`, `video_url`, `caption`, `share_to_feed`, `cover_url` or `thumb_offset` [ms]) → `GET /{container}?fields=status_code` (`IN_PROGRESS/FINISHED/ERROR/EXPIRED/PUBLISHED`) → `POST /{ig-user-id}/media_publish creation_id` → media id | ✔ |
| Polling | "once per minute, for no more than 5 minutes"; containers expire after 24 h | poll 30 s→60 s, `IG_POLL_TIMEOUT_S=600` (Reels processing can exceed 5 min; EXPIRED handled) |
| Resumable upload | **"Only for apps that have implemented Facebook Login for Business"** (rupload.facebook.com) | default **`IG_UPLOAD_METHOD=r2_url`**; resumable path implemented per docs but expected to fail with Instagram Login (marked) |
| Caption | max 2200 chars, 30 hashtags, 20 @tags | ✔ (we use 3–5 tags, 1 @) |
| Publish limit | conflicting: guide says 100/24 h, reference says 50 (`content_publishing_limit` → `quota_usage`, `config.quota_total`) | checked before publish; we post ≤2/day anyway |
| Container limit | 400 containers / 24 h | n/a |
| OAuth | `https://www.instagram.com/oauth/authorize?client_id&redirect_uri&response_type=code&scope&state`; code valid 1 h, strip trailing `#_`; `POST https://api.instagram.com/oauth/access_token` (form, `grant_type=authorization_code`) | ✔ |
| Long-lived token | `GET graph.instagram.com/access_token?grant_type=ig_exchange_token&client_secret&access_token`, 60 days | ✔ |
| Refresh | `GET graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token`; token must be ≥24 h old and unexpired; new 60 days | ✔ (refresh at <10 days left) |
| Scopes | `instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_comments`, `instagram_business_manage_insights` | ✔ |
| Comments | `GET /{media}/comments`, `POST /{comment}/replies {message}`, `POST /{comment}` hide/unhide | ✔; **hide parameter name `hide=true` NOT verified** (from memory) |
| Reels insights | valid: `views` (in development), `reach`, `likes`, `comments`, `shares`, `saved`, `total_interactions`, `ig_reels_avg_watch_time`; deprecated: `plays`, `impressions`, `clips_replays_count`, `ig_reels_aggregated_all_plays_count` | ✔ |
| Account insights | `reach` (day), `follower_demographics` (lifetime, timeframe, breakdown=country, ≥100 followers); `online_followers` not in the current table → optional | follower count from `GET /me?fields=followers_count` (**field not re-verified**) |
| Copyright | container `copyright_check_status {status, matches_found}`; media `copyright_check_information` | publish aborted + source blacklisted if `matches_found=true` |
| Video spec | MP4/MOV, moov at front, H.264/HEVC closed GOP 4:2:0, 23–60 fps, ≤25 Mbps VBR, AAC ≤48 kHz 1–2 ch, 3 s–15 min, ≤300 MB | our 1080×1920/60/8 Mbps/AAC 48k/faststart matches |
| Deprecation headers | not documented on these pages | client scans response headers for `deprecat`/`sunset`/`warning` (best effort, unverified) |

Error classification (codes from general Graph API knowledge, **not re-verified tonight**):
190 → AUTH; 10 / 200–299 → PERMISSION; 4, 17, 32, 613, 80002 → RATE_LIMIT; subcode 2207042 →
PUBLISH_LIMIT; 368 / subcode 2207051 / "spam"/"blocked"/"restricted" → ACTION_BLOCKED;
5xx, 1, 2 → TRANSIENT; 9007 / 2207027 → NOT_READY.

## D-021 Backups never contain the Fernet key
- **Karar:** `backups/daily/automation_YYYYMMDD.db` (14 kept) holds tokens/passwords only in
  encrypted form; `.env`/FERNET_KEY is not copied. Emre must keep FERNET_KEY in a password
  manager — without it the tokens in any backup are unreadable (re-connect accounts).

## D-022 Processes
- **Karar:** 5 processes: `cycle_runner.py` (production + 6-hourly fast lane — the only GPU
  producer), `publish_runner.py` (publishing, token refresh, insights, maintenance),
  `comment_responder.py`, `panel.py`, `watchdog.py`. The fast lane lives in the cycle runner so
  hours of GPU work never block publishing.

## D-023 Panel OAuth redirect
- **Karar:** Meta requires an HTTPS redirect URI; the panel is plain HTTP on 127.0.0.1. The panel
  offers `/oauth/instagram/callback` *and* a "paste the final URL" form, so Emre can register
  `https://localhost:5050/oauth/instagram/callback`, let the browser fail to load it, and paste the
  address bar URL (code + state are validated the same way).

## D-024 YouTube 403 / PO token = systemic block
- **Karar:** During the E2E run a real download failed: `android_vr client https formats require a
  GVS PO Token … ERROR: unable to download video data: HTTP Error 403` (yt-dlp 2026.08.19).
  This is now classified as `YouTubeBlocked("FORBIDDEN_403_PO_TOKEN")` → downloads stop for the
  cycle + CRITICAL alert, instead of burning every source video as "failed".
- **Not done:** `pip install -U yt-dlp` was NOT run tonight (it would change the Python env shared
  with other pipelines). cycle_runner does it once per cycle when it runs.
- **E2E fallback:** synthetic testsrc footage + local Windows SAPI speech.

## D-010 requirements
- **Karar:** No `requirements.txt` existed; created a curated UTF-8 one (pinned to installed
  versions). The old UTF-16 freeze `requirements_working_20260522.txt` was converted to UTF-8.

## D-025 YouTube access method (2026-10-22, verified on this PC)
- **Karar:** Downloads, ytsearch, channel listings and latest-upload checks all run through the project
  `.venv` (yt-dlp 2026.10.21 + yt-dlp-ejs 0.8.0) with **deno** as JS runtime (portable,
  `tools\deno\deno.exe`, passed by full path) and yt-dlp's **default player clients**, **without cookies**
  and without forcing a PO token. Verified: 3/3 probe downloads + production-path verification
  (docs/YTDLP_FIX.md).
- **Gerekçe:** First fully working combination in the requested order (a → b → c → d); stopped there.
  The old 403 came from yt-dlp 2026.08.19 + our code forcing `player_client=android_vr,web,tv`
  (upstream removed android_vr from defaults in 2026.08.19; 2026.10.21 made `tv_samsung` a default).
  EJS wiki recommends deno (node >= 22 also supported → `YTDLP_JS_RUNTIME=node`).
- **Alternatif (yedek, doğrulanmadı):** `YTDLP_PLAYER_CLIENT=mweb` + bgutil PO-token server
  (PO-Token-Guide recommendation); the server stays as the `Reels-PotProvider` autostart task.

## D-026 Cookies off by default
- **Karar:** `YTDLP_USE_COOKIES=false`. cookies.txt is no longer sent; the cookie-age alarm only runs
  when cookies are enabled.
- **Gerekçe:** Not needed for public videos (verified) and the yt-dlp wiki warns that account cookies
  risk the account being banned; the existing file is ~2 months old (exported ~12 Aug).

## D-027 yt-dlp always from the venv; full paths for tools
- **Karar:** `downloader.ytdlp_python()` = `YTDLP_PYTHON` → `.venv\Scripts\python.exe` → current python;
  the per-cycle update runs `pip install -U "yt-dlp[default]" bgutil-ytdlp-pot-provider` in that venv
  (`YTDLP_UPDATE_CHANNEL=nightly` adds `--pre`). deno/node/ollama are called by full path
  (`OLLAMA_EXE`, `YTDLP_JS_RUNTIME_PATH`) because this PC's PATH lacks System32 and nodejs.
- **Not changed:** the system PATH (Windows setting — fix steps in docs/YTDLP_FIX.md).

## D-028 Publisher timestamps follow the injected clock
- **Karar:** `publish_item` stamps container/publish times as `now + elapsed wall time` instead of
  `max(now, utcnow())`. Found because a test started failing once the real date passed the test's
  fixed `NOW`; the old form mixed two clocks.

## D-029 Full test (2026-10-22): niches, download budget, order
- **Karar:** The DB copy has a single sector (`IG_deneme31316969`, the real test account's). The 4 test
  niches come from the original project's sector set (bg_music folders) with clean names for prompts:
  Podcast, Personal Finance, Tech / AI, Psychology (music_slug → the `*_riskli` folders).
- **Karar:** YouTube budget 8 downloads = 1 (1c probe) + 3 speech samples in A (max 1 per niche; the
  4th niche's candidates stay CANDIDATE with reason "sample skipped: budget") + 4 source videos in C
  (1 per niche, up to 3 clips each = 12 reels). Discovery runs with 3 candidates per niche (not the
  production 5/10) to stay polite. The production `plan_production` would pick ~3 videos per niche for a
  2-cycle buffer; the harness limits it to 1.
- **Karar:** 1e (model comparison) runs after A, using A's real Whisper sample transcripts as inputs,
  so no extra download is needed. A itself runs with qwen3:32b (current default).

## D-030 Model: qwen3.8:27b, OLLAMA_NUM_GPU=99, OLLAMA_NUM_CTX=12288 (full test 1e, 2026-10-22)
Same inputs for both models (2 real Whisper samples from step A + 10 comments incl. 2 injections):

| | qwen3:32b (auto) | qwen3.8:27b (num_gpu 99) |
|---|---|---|
| `ollama ps` ctx 6144 | 100 % GPU, 21 GB | 100 % GPU, 17 GB (auto placement: 6 %/94 % CPU/GPU) |
| `ollama ps` ctx 8192 | **4 %/96 % CPU/GPU** | 100 % GPU |
| ctx 10240 / 12288 | — | 100 % GPU; ~2.7 GB free per card at 12288 |
| think leakage | none | none (`think:false` works) |
| JSON first-try | 18/19 (1 retry) | 19/19 |
| avg / max call | 6.1 s / 45 s | 5.7 s / 38 s |
| generation | 11.3 tok/s | 18.9 tok/s |
| clips found | 2 (PF) + **0** (Psychology) | 3 (PF) + 2 (Psychology) |
| episode-title question | **invented a title** | "I don't have the title…" |
| Turkish comment | English, off-topic | answered in Turkish |

- **Karar:** qwen3.8:27b with `OLLAMA_NUM_GPU=99` (Ollama alone leaves 6 % on CPU) and
  `OLLAMA_NUM_CTX=12288` (largest tested value that stays 100 % on GPU; KV cache adds only ~130 MB/card
  from 6144→12288). Clip-scoring prompts peak at ~2.4k tokens, so nothing was truncated at any setting.
- **Gerekçe:** better grounding (no invented facts), more clips found, 1.7× faster generation, 0 JSON retries.
- **Alternatif:** qwen3:32b at 6144 (works, slower, hallucinated once).

## D-031 Full test: what deviated from the plan and why
- **A harness bug:** the speech-sample budget was capped per niche but not to 3 in total, so A took 4
  samples. With the 8-download limit kept, C had 3 source videos instead of 4. One of them (TEDx,
  Psychology) got a 403 PO-token error → **6 reels instead of 12**. No extra downloads were made to make
  up for it (the 8-video limit is a test rule); the remaining steps ran on these 6 reels.
- **Order:** 1e after A (uses A's real Whisper samples), B after D/E (B-priority needs the scheduled
  queue snapshot and a rendered reel to reuse, since the download budget was spent).
- **Panel test port:** 5071 (5061 is on Chromium's unsafe-port list → Playwright `ERR_UNSAFE_PORT`).
- **Real DB:** the SQLite backup API read at setup opened the WAL-mode DB, which left empty
  `automation.db-wal` (0 bytes) / `-shm` files; no write (hash unchanged). Removed in J.

## D-032 Scheduler vs. rolling 24 h cap — only the publisher deferral fixed
- **Bulgu:** `schedule_account` limits posts per Warsaw window-day; `guardrails.can_publish` counts a
  rolling 24 h. Posts planned on consecutive days < 24 h apart are deferred at publish time (step D/E:
  3 of 6 planned slots were < 24 h after the account's previous post).
- **Karar (bug, fixed in d458134):** a DEFER_CAP moved the item to the NEXT day's window start (a whole
  day lost, landing on that day's planned post → cascade). It now waits until the rolling cap frees up
  (`guardrails.cap_frees_at`), inside the window, else the next window start. E: 4/6 → 6/6 published.
- **Not changed (design, reported):** making the scheduler itself respect the rolling 24 h was tried and
  reverted: with "random minute inside the slot" posts drift later every day and whole days get skipped
  (8 days → 6 posts in the existing test). Choosing between plan-accuracy, randomness and throughput is
  Emre's call — see FULL_TEST_REPORT "Sadece rapor".

## D-033 20-account projection fits → qwen3:30b-a3b test skipped
- C measured ~219 s of machine time per reel (download+Whisper+Qwen+render+RIFE+QC). 60 reels per 3-day
  cycle ≈ 4–6.5 h of GPU work per 72 h (depending on source length), so the "doesn't fit" condition for
  trying qwen3:30b-a3b was not met; no model pull was made.

## D-034 Clip boundaries on sentences (Emre's decision #1, 2026-10-23)
- **Kural (Emre):** start ≤ 3 s back / ≤ 2 s forward to a sentence start (previous word ends . ? ! or
  ≥ 0.6 s silence); end ≤ 4 s forward / ≤ 3 s back to a sentence end + 0.25 s; length 18–32 s; no
  boundary or out of range → rejected and logged, next candidate tried; 0.12 s audio fades.
- **Karar (details left open):** a boundary within ±0.5 s of Qwen's time counts as "already there"
  (Qwen only gives whole seconds, so 6.4 s should not be pushed back to an earlier sentence); the
  0.25 s tail never runs into the next word; the start is the word's own start (no pre-roll); the first
  word of a segment is a sentence start only after ≥ 0.6 s of silence; the last word only ends a sentence
  with punctuation. Qwen's raw length pre-filter is 18–32 s ± 3 s (alignment can move each end by a
  few seconds); the exact 18–32 s check is after alignment. Overlaps are resolved AFTER alignment, so a
  rejected best clip does not take its overlapping alternatives with it. The old 1 s fade-out was
  replaced by the 0.12 s one (it would swallow the now complete last word).
- **Gerçek veri (C'nin Jay Shetty klipleri):** 2/3 now end on a full sentence ("…long after the story
  ends.", "…already within you."), 1/3 rejected (no sentence end within +4/−3 s); render + QC pass.

## D-035 Daily cap per posting-window day + same-account gap (Emre's decision #2, 2026-10-23)
- **Kural (Emre):** cap = N posts per account per Warsaw posting-window day (day = the window's start
  date); `MIN_GAP_SAME_ACCOUNT` cap 1 → 14 h, cap 2 → 6 h; the global 8 min stays; Qwen cannot change
  any of it. Replaces the rolling 24 h count (D-032).
- **Karar (details):** outside the window (05:00–15:00) a post counts in the window that started most
  recently, so a late publish cannot open a "new day". `can_publish` checks both rules; on either the
  publisher defers to the earliest time both pass (inside the window, else the next window start).
  The scheduler applies the same rules when planning (random minute kept, slot clipped to
  "previous post + gap", later posts checked too) and so does the missed-post re-spread. The planner
  keeps planned posts 10 min apart (8 min guardrail + 2 min) because a publish is stamped a little after
  its slot; exactly 8 min apart was deferred once in the 8-day simulation.
- **Sonuç:** 8-day simulation (20–27 Oct incl. the DST night), 4 accounts × 5 seeds: exactly one post per
  window day, ≥ 14 h apart, 32/32 published at their planned time, 0 days lost.

## D-036 Comment replies in the curator's voice (Emre's decision #3, 2026-10-23)
- **Karar (details):** opinions in the curator's own voice stay allowed ("I agree…", "I'm not sure…",
  "I can't say…"); the validator rejects first-person EXPERIENCE/CLAIM patterns (we/us/our, "I don't /
  always / never / just / did / used to / paid / made…", "I've", "my wife / kids / business / mortgage /
  show / guest…"), redirects (link, description, bio, DM, full episode/video/interview), "account owner"
  and off-topic dodges. "full episode" is rejected even inside an honest answer (Emre's list), so a
  question about the episode name may end without a reply. SOURCE block = channel title + handle +
  clip topic (the speaker's name is not stored, so it is not given). One retry, then no reply.

## D-037 One 403 no longer stops the cycle (Emre's decision #4, 2026-10-23)
- **Kural (Emre):** a 403 on one video → wait 60–120 s, retry once with `bv*[vcodec^=avc1][height<=1080]+ba/b`;
  still 403 → only that video fails. YOUTUBE_BLOCKED (stop downloads + CRITICAL alert) only after 403/bot
  errors on ≥ 3 different videos within 1 hour; "Sign in to confirm you're not a bot" stays an instant block.
- **Karar (details):** 429 / "too many requests" count as strikes like 403 (they were instant blocks before);
  cookie errors stay instant blocks (configuration problem). Strikes are stored in system_state
  (`yt_forbidden_strikes`, video id + time), so they survive service restarts and are shared by the cycle,
  discovery samples and the fast lane; the same video counts once. Audio samples (discovery) count strikes
  but get no format retry. New block kind `FORBIDDEN_403_REPEATED` with its own fix hint.

## D-038 Source selection rules + clip ranking (Emre's decision #5, 2026-10-23)
- **Düzeltme (rapor hatası):** FULL_TEST_REPORT'ta "Queen (müzik kanalı)" dedim; yanlış. A'daki "Queen"
  (UCGjj4ULTpqgcM6knuBv9h6Q) bir psikoloji/felsefe anlatım kanalı (Carl Jung, procrastination…), 12 videodan
  2'si şarkı yüklemesi; açıklamada "Narration: Eternalised (Full Rights)" → başkasının anlatımını yeniden
  yüklüyor. Psikoloji uyumu 0,85 makuldü; asıl sorun yeniden yükleme. Müzik kuralı yine uygulandı (Emre'nin
  kararı) ve gerçek bir müzik kanalı örneğiyle test edildi; Qwen format değerlendirmesine "reupload" eklendi.
- **Karar (details):** the exclusion list lives in config (`SOURCE_EXCLUDE_CHANNEL_IDS`, `SOURCE_EXCLUDE_NAME_PATTERNS`,
  .env can override), with the channel ids YouTube reported in step A (TED, TEDx, TED-Ed, Bloomberg TV);
  "TED" is matched case-sensitively (Ted Lasso fan channels are not TED); any channel with "News" or
  "Television" in its name counts as a news/TV channel (broad on purpose). Code rules run before any model
  call and before the category request; the category comes from the sample video's metadata (no download).
  Reaction share uses title + description of the last 15 videos. Clip permission: code regex on the
  description first, then the model's verdict ONLY with a quote that really occurs in the description;
  otherwise None ("unknown") and the score is rescaled without that term. Saturation: distinct other
  channels whose result titles contain all words of the normalized name, over "<name> clips / podcast
  clips / shorts". Clip scores: 1–100 per segment (no ties in one answer, < 60 dropped) + one ranking call
  per video over all aligned candidates (strictly decreasing, no ties); clips are picked in ranking order.
  If the ranking call fails, the segment scores are kept. `videos.qwen_point` is now on the 1–100 scale
  (older rows 0–9; only used for ordering).
- **Ölçüm:** A2 (qwen3.8:27b, 4 ses örneği): TED dışlama listesiyle, "Too Late Now" Qwen'le
  (reaction_compilation) elendi; doygunluk 1–7 kanal ölçüldü (eskiden hep 0); izin her kaynakta "bilinmiyor".
  Gerçek transkriptte sıralama: 10 aday 98→48, tekrar eden puan 0.

## D-039 Small fixes from the full test (Emre's decision #6, 2026-10-23)
- `pause_reason` no longer doubles the error kind ("AUTH: AUTH: …"); `COPYRIGHT_MATCH` e-mails; a watchdog
  restart of Ollama writes a log line + `OLLAMA_RESTARTED` WARNING alert (not an e-mail kind).
- Credit line: the handle is kept exactly and the line ends with " on YouTube"
  (`🎥 @JayShettyPodcast. on YouTube`), also for the name fallback.
- `scripts/stop_services.ps1`: stops running Reels-* scheduled tasks (Stop-ScheduledTask, never removes them)
  and kills every repo service process with its whole child tree via ParentProcessId (the .venv python.exe is
  a launcher with the real interpreter as child), watchdog first; Ollama is not touched. Pure PowerShell —
  no taskkill (this machine's sessions may lack System32 in PATH); `-DryRun` lists only. Live-tested with a
  dummy 3-process tree: all stopped, exit 0, other processes untouched.
