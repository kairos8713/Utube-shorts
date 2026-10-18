import datetime
import secrets

from flask import Flask, abort, request, session

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

    def csrf_token():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_hex(16)
        return session["_csrf"]

    @app.before_request
    def _csrf_protect():
        if request.method == "POST" and not app.config.get("CSRF_DISABLED"):
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
            if not sent or sent != session.get("_csrf"):
                abort(400, "CSRF token missing or invalid")

    from timeutil import fmt_local
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.filters["local"] = lambda dt, fmt="%Y-%m-%d %H:%M": fmt_local(dt, fmt) if dt else "—"

    from web.auth import auth_bp
    from web.views import main_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    return app
