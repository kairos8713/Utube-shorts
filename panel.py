"""
panel.py — run the web panel.

Binds to 127.0.0.1 by default (PANEL_HOST to change). Never runs in debug mode.
"""
from config import settings
from log_setup import get_logger
from web import create_app

log = get_logger("panel")
app = create_app()

if __name__ == "__main__":
    if not settings.PANEL_PASSWORD_HASH:
        log.warning("PANEL_PASSWORD_HASH is empty — login disabled. "
                    "Run: python scripts/init_secrets.py --set-panel-password")
    log.info("Panel on http://%s:%s", settings.PANEL_HOST, settings.PANEL_PORT)
    app.run(host=settings.PANEL_HOST, port=settings.PANEL_PORT, debug=False, use_reloader=False)
