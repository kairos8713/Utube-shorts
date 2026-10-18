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

## D-010 requirements
- **Karar:** No `requirements.txt` existed; created a curated UTF-8 one (pinned to installed
  versions). The old UTF-16 freeze `requirements_working_20260522.txt` was converted to UTF-8.
