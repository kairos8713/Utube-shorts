# legacy_youtube/

Archived YouTube-era code (2026-10-19 Instagram migration). **Not imported by any active
module** and not maintained; kept for reference only. Several files no longer run as-is
because the modules they import (e.g. `legacy_runner`, `publisher`) live here now and the
DB schema moved on.

- `publisher.py` — Playwright YouTube Studio uploader
- `weekly_cycle.py`, `sector_processor.py`, `fulfillment_engine.py` — old 7-day orchestrators
  (replaced by `cycle_runner.py` + `production.py`)
- `legacy_runner.py` — old monolith (MoviePy renderer, Kokoro TTS, transformers/AWQ Qwen);
  its still-useful parts were split into `downloader.py`, `transcriber.py`, `media_utils.py`
- `update_stats.py`, `verify_login.py`, `get_session_file.py`, `setup_tokens.py` — YouTube auth/stats
- `web_routes.py`, `web_app.py`, `web_templates/` — old panel (had hard-coded `root/toor` login)
- one-off `debug_*`, `fix_*`, `migrate_*`, `check_*` scripts

`youtube_analytics.py` / `fetch_channel_stats.py` were not present in this working copy.
