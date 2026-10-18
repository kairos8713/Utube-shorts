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
