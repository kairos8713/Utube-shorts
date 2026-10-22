"""
email_notifier.py — emergency e-mails only.

* Settings from .env (SMTP_*, EMAIL_FROM, EMAIL_TO, EMAIL_ENABLED, EMAIL_TEST_MODE).
* EMAIL_TEST_MODE=true (default) → messages are written to logs/outbox/*.eml
  instead of being sent.
* De-duplication: at most one e-mail per `kind` (+ optional key) every 6 hours.
"""
import datetime as _dt
import json
import os
import smtplib
import threading
import uuid
from email.message import EmailMessage

from config import settings
from log_setup import get_logger

log = get_logger("email")

DEDUP_HOURS = 6
_lock = threading.Lock()

# The only kinds that may e-mail (task: "sadece acil durumlar").
EMAIL_KINDS = {
    "ACCOUNT_AUTO_PAUSED", "TOKEN_REFRESH_FAILED", "TOKEN_EXPIRING", "WATCHDOG",
    "SERVICE_RESTART_FAILED", "DISK_LOW", "YOUTUBE_BLOCKED", "COOKIES_STALE",
    "RISKY_COMMENT", "API_DEPRECATION", "BACKUP_FAILED", "PUBLISH_UNCERTAIN", "FFMPEG_TOO_OLD", "R2_NOT_CONFIGURED", "PUBLISH_STUCK",
    "COPYRIGHT_MATCH",
}


def _state_path() -> str:
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    return os.path.join(settings.LOG_DIR, "email_dedup.json")


def _load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict):
    with open(_state_path(), "w", encoding="utf-8") as f:
        json.dump(state, f)


def should_send(dedup_key: str, now: _dt.datetime = None) -> bool:
    now = now or _dt.datetime.utcnow()
    with _lock:
        state = _load_state()
        last = state.get(dedup_key)
        if last and now - _dt.datetime.fromisoformat(last) < _dt.timedelta(hours=DEDUP_HOURS):
            return False
        state[dedup_key] = now.isoformat()
        _save_state(state)
        return True


def send_alert(kind: str, subject: str, body: str, key: str = "", now: _dt.datetime = None) -> str:
    """Returns 'sent' | 'outbox' | 'deduped' | 'disabled' | 'not_emergency' | 'error'."""
    if kind not in EMAIL_KINDS:
        return "not_emergency"
    if not settings.EMAIL_ENABLED:
        log.info("email disabled: %s %s", kind, subject)
        return "disabled"
    if not should_send(f"{kind}:{key}", now):
        log.info("email deduped (%sh): %s %s", DEDUP_HOURS, kind, key)
        return "deduped"

    from redact import redact
    subject, body = redact(subject), redact(body)
    msg = EmailMessage()
    msg["From"] = settings.EMAIL_FROM or settings.SMTP_USER or "reels-bot@localhost"
    msg["To"] = settings.EMAIL_TO or settings.EMAIL_FROM or "emre@localhost"
    msg["Subject"] = f"[Reels Bot][{kind}] {subject}"
    stamp = (now or _dt.datetime.utcnow()).strftime("%Y-%m-%d %H:%M UTC")
    msg.set_content(f"{body}\n\n---\n{stamp}\nOtomatik bildirim — Reels otomasyonu")

    if settings.EMAIL_TEST_MODE:
        outbox = os.path.join(settings.LOG_DIR, "outbox")
        os.makedirs(outbox, exist_ok=True)
        path = os.path.join(outbox, f"{_dt.datetime.utcnow():%Y%m%dT%H%M%S}_{kind}_{uuid.uuid4().hex[:6]}.eml")
        with open(path, "wb") as f:
            f.write(bytes(msg))
        log.info("email → outbox %s", path)
        return "outbox"
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.send_message(msg)
        log.info("email sent: %s", subject)
        return "sent"
    except Exception as e:
        log.error("email failed: %s", e)
        return "error"
