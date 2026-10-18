"""
instagram/token_refresh.py — daily job.

* Tokens with < 10 days left are refreshed (Meta requires the token to be
  ≥ 24 h old and unexpired). Failure → alert + e-mail.
* < 7 days left (refresh failing) → TOKEN_EXPIRING alert + e-mail every day.
* DRY_RUN: the refresh request is not sent (logged only).
"""
import datetime as _dt

from alerts import raise_alert
from config import settings
from crypto_utils import decrypt
from instagram import oauth
from log_setup import get_logger
from models import OwnedChannels
from timeutil import utcnow

log = get_logger("token_refresh")

REFRESH_WHEN_LEFT = _dt.timedelta(days=10)
WARN_WHEN_LEFT = _dt.timedelta(days=7)
MIN_AGE = _dt.timedelta(hours=24)


def run(session, now=None, refresh_fn=None, dry_run=None) -> dict:
    now = now or utcnow()
    dry = settings.DRY_RUN if dry_run is None else dry_run
    refresh_fn = refresh_fn or oauth.refresh_long_lived
    out = {"refreshed": [], "failed": [], "warned": [], "skipped": []}
    accounts = session.query(OwnedChannels).filter(OwnedChannels.ig_access_token != None).all()  # noqa: E711
    for acc in accounts:
        exp = acc.ig_token_expires_at
        if not exp:
            continue
        left = exp - now
        if left <= _dt.timedelta(0):
            acc.status = "DISCONNECTED"
            acc.pause_reason = "access token expired — reconnect in the panel"
            raise_alert("TOKEN_REFRESH_FAILED", f"Account {acc.ig_username or acc.id}: token EXPIRED, reconnect it",
                        severity="CRITICAL", account_id=acc.id, session=session)
            out["failed"].append(acc.id)
            continue
        if left > REFRESH_WHEN_LEFT:
            continue
        issued = acc.ig_token_refreshed_at
        if issued and now - issued < MIN_AGE:
            out["skipped"].append(acc.id)
            continue
        if dry:
            log.info("[DRY_RUN] would refresh token of account %s (%s left)", acc.id, left)
            out["skipped"].append(acc.id)
        else:
            try:
                res = refresh_fn(decrypt(acc.ig_access_token))
                oauth.store_token(acc, res["access_token"], res["expires_in"], now)
                out["refreshed"].append(acc.id)
                log.info("token refreshed for account %s", acc.id)
                continue
            except Exception as e:
                out["failed"].append(acc.id)
                raise_alert("TOKEN_REFRESH_FAILED", f"Account {acc.ig_username or acc.id}: token refresh failed: {e}",
                            severity="CRITICAL", account_id=acc.id, session=session)
        if left < WARN_WHEN_LEFT:
            # one e-mail per day: the dedup window is 6 h, the alert message changes daily
            raise_alert("TOKEN_EXPIRING", f"Account {acc.ig_username or acc.id}: token expires in "
                        f"{left.days} days ({exp:%Y-%m-%d}) on {now:%Y-%m-%d}", severity="CRITICAL",
                        account_id=acc.id, session=session)
            out["warned"].append(acc.id)
    session.commit()
    return out
