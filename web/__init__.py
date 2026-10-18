import datetime
import secrets

from flask import Flask

from config import settings


def create_app(testing: bool = False):
    app = Flask(__name__)
    key = settings.PANEL_SECRET_KEY
    if not key:
        # No key configured: use an ephemeral one (sessions reset on restart).
        key = secrets.token_hex(32)
    app.secret_key = key
    app.config.update(
        TESTING=testing,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12),
    )

    from web.auth import auth_bp
    from web.views import main_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    return app
