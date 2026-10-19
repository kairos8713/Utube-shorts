"""
instagram/oauth.py — Business Login for Instagram (multi-account).

Panel "Hesap Bağla" → authorize_url(state) → Instagram → redirect to
IG_REDIRECT_URI?code=...&state=... → complete_login() exchanges the code for a
short-lived token, then a 60-day long-lived token, and stores it ENCRYPTED on
the account row. Endpoints verified 2026-10-19 (DECISIONS D-020).
"""
import datetime as _dt
import secrets
from urllib.parse import urlencode

from config import settings
from crypto_utils import encrypt
from instagram.errors import IGError, classify
from log_setup import get_logger
from redact import redact, register_secret
from timeutil import utcnow

log = get_logger("ig_oauth")

AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
CODE_EXCHANGE_URL = "https://api.instagram.com/oauth/access_token"
LONG_LIVED_URL = "https://graph.instagram.com/access_token"
REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
SCOPES = ("instagram_business_basic", "instagram_business_content_publish",
          "instagram_business_manage_comments", "instagram_business_manage_insights")


def new_state() -> str:
    return secrets.token_urlsafe(24)


def authorize_url(state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": settings.IG_APP_ID, "redirect_uri": settings.IG_REDIRECT_URI,
        "response_type": "code", "scope": ",".join(SCOPES), "state": state, "enable_fb_login": "false",
    })


def _http(method, url, params=None, data=None):
    import requests
    try:
        r = requests.request(method, url, params=params, data=data, timeout=60)
    except requests.exceptions.RequestException as e:
        # the message contains the URL with code / client_secret / access_token
        raise IGError("TRANSIENT", f"network: {redact(e)}") from None
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:300]}
    if r.status_code >= 400 or (isinstance(body, dict) and body.get("error")):
        raise classify(r.status_code, body if isinstance(body, dict) else {})
    return body


def exchange_code(code: str, http=_http) -> dict:
    code = code.split("#")[0]                    # docs: strip trailing "#_"
    body = http("POST", CODE_EXCHANGE_URL, data={
        "client_id": settings.IG_APP_ID, "client_secret": settings.IG_APP_SECRET,
        "grant_type": "authorization_code", "redirect_uri": settings.IG_REDIRECT_URI, "code": code})
    if "data" in body:                           # newer response shape: {"data": [{...}]}
        body = body["data"][0]
    return {"access_token": body["access_token"], "user_id": str(body.get("user_id", ""))}


def exchange_long_lived(short_token: str, http=_http) -> dict:
    body = http("GET", LONG_LIVED_URL, params={"grant_type": "ig_exchange_token",
                                               "client_secret": settings.IG_APP_SECRET,
                                               "access_token": short_token})
    return {"access_token": body["access_token"], "expires_in": int(body.get("expires_in", 5184000))}


def refresh_long_lived(token: str, http=_http) -> dict:
    body = http("GET", REFRESH_URL, params={"grant_type": "ig_refresh_token", "access_token": token})
    return {"access_token": body["access_token"], "expires_in": int(body.get("expires_in", 5184000))}


def store_token(account, token: str, expires_in: int, now=None):
    now = now or utcnow()
    register_secret(token)
    account.ig_access_token = encrypt(token)
    account.ig_token_expires_at = now + _dt.timedelta(seconds=expires_in)
    account.ig_token_refreshed_at = now


def complete_login(session, account, code: str, http=_http, me_fn=None) -> None:
    """Callback handler body. The account keeps its status (WARMUP) until the warm-up gate passes."""
    if not settings.IG_APP_ID or not settings.IG_APP_SECRET:
        raise IGError("PERMISSION", "IG_APP_ID / IG_APP_SECRET missing in .env")
    short = exchange_code(code, http)
    long_ = exchange_long_lived(short["access_token"], http)
    store_token(account, long_["access_token"], long_["expires_in"])
    account.ig_user_id = short["user_id"] or account.ig_user_id
    if me_fn:
        info = me_fn(long_["access_token"])
        account.ig_user_id = str(info.get("user_id") or account.ig_user_id)
        account.ig_username = info.get("username") or account.ig_username
    if account.status in (None, "DISCONNECTED"):
        account.status = "WARMUP"
    session.commit()
    log.info("account %s connected (ig_user_id=%s)", account.id, account.ig_user_id)
