"""
alerts.py — panel alarm rows (+ e-mail for emergency kinds).

raise_alert() creates an unresolved Alert row (shown in the red alarm area on the
panel home page) unless an identical unresolved one exists, and e-mails if the
kind is an emergency kind (deduplicated in email_notifier).
"""
from email_notifier import EMAIL_KINDS, send_alert
from log_setup import get_logger
from models import Alert, Session
from timeutil import utcnow

log = get_logger("alerts")


def raise_alert(kind: str, message: str, severity: str = "WARNING", account_id: int = None,
                email: bool = True, session=None) -> Alert:
    own = session is None
    s = session or Session()
    try:
        existing = (s.query(Alert).filter_by(kind=kind, owned_channel_id=account_id, resolved=False,
                                             message=message).first())
        if existing:
            return existing
        a = Alert(kind=kind, message=message[:2000], severity=severity, owned_channel_id=account_id,
                  created_at=utcnow())
        s.add(a)
        s.commit()
        log.warning("ALERT %s [%s] acc=%s: %s", kind, severity, account_id, message)
        if email and kind in EMAIL_KINDS:
            status = send_alert(kind, message[:120], message, key=str(account_id or ""))
            a.emailed = status in ("sent", "outbox")
            s.commit()
        return a
    finally:
        if own:
            s.close()


def resolve_alert(alert_id: int, session=None):
    own = session is None
    s = session or Session()
    try:
        a = s.get(Alert, alert_id)
        if a and not a.resolved:
            a.resolved = True
            a.resolved_at = utcnow()
            s.commit()
    finally:
        if own:
            s.close()


_RANK = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}


def open_alerts(session, limit: int = 50):
    rows = session.query(Alert).filter_by(resolved=False).order_by(Alert.created_at.desc()).limit(limit).all()
    return sorted(rows, key=lambda a: _RANK.get(a.severity, 3))
