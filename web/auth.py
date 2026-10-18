"""
web/auth.py — single-user panel login.

* Username + werkzeug password hash come from .env (PANEL_USERNAME,
  PANEL_PASSWORD_HASH). With no hash configured, login is disabled.
* Simple in-memory rate limit: 5 failed attempts per IP → 15 min lockout.
"""
import threading
import time
from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from config import settings

auth_bp = Blueprint("auth", __name__)

MAX_FAILURES = 5
WINDOW_S = 15 * 60

_failures = {}          # ip -> [timestamps]
_lock = threading.Lock()


def _recent_failures(ip: str, now: float) -> list:
    with _lock:
        hits = [t for t in _failures.get(ip, []) if now - t < WINDOW_S]
        _failures[ip] = hits
        return hits


def _record_failure(ip: str, now: float):
    with _lock:
        _failures.setdefault(ip, []).append(now)


def reset_rate_limit():
    with _lock:
        _failures.clear()


def is_locked_out(ip: str) -> bool:
    return len(_recent_failures(ip, time.time())) >= MAX_FAILURES


def check_credentials(username: str, password: str) -> bool:
    pw_hash = settings.PANEL_PASSWORD_HASH
    if not pw_hash:
        return False
    if (username or "") != settings.PANEL_USERNAME:
        # Still run a hash check so timing does not reveal the username.
        check_password_hash(pw_hash, password or "")
        return False
    return check_password_hash(pw_hash, password or "")


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("auth.login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    configured = bool(settings.PANEL_PASSWORD_HASH)
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if is_locked_out(ip):
            flash("Çok fazla hatalı deneme. 15 dakika sonra tekrar deneyin.", "danger")
            return render_template("login.html", configured=configured), 429
        if check_credentials(request.form.get("username"), request.form.get("password")):
            session.clear()
            session["logged_in"] = True
            session.permanent = True
            nxt = request.args.get("next") or ""
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("main.home")
            return redirect(nxt)
        _record_failure(ip, time.time())
        flash("Hatalı kullanıcı adı veya şifre.", "danger")
    return render_template("login.html", configured=configured)


@auth_bp.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
