# legacy_misc/

Archived non-YouTube code that the new system replaces. Not imported by active code.

- `daily_production.py`, `publish_on_connect.py` — earlier IG attempt using the unofficial
  `instagrapi` private API + ADB airplane-mode IP rotation over USB tethering. Replaced by the
  official Instagram API with Instagram Login (`instagram/`). Private-API automation risks
  account bans and breaks Meta's terms; it must not be revived.
- `ig_dashboard.py`, `ig_templates/`, `ig_static/` — its unauthenticated panel (port 5001)
- `migrate_ig_columns.py` — superseded by `db_migrate.py`
- `esrgan_upscaler.py` — 4K upscale step, unused (delivery is 1080×1920)
- `test_render.py` — script for the old 3-stage 72 fps render
