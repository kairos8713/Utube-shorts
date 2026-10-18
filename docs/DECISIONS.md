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

## D-010 requirements
- **Karar:** No `requirements.txt` existed; created a curated UTF-8 one (pinned to installed
  versions). The old UTF-16 freeze `requirements_working_20260522.txt` was converted to UTF-8.
