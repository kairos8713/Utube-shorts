"""
instagram/errors.py — classify Graph API errors into actionable kinds.

Codes come from general Graph API conventions (see DECISIONS D-020: not all
re-verified). Unknown errors are TRANSIENT only for 5xx; otherwise UNKNOWN.
"""

AUTH = "AUTH"                  # token invalid/expired → pause account, alert
PERMISSION = "PERMISSION"      # missing scope / not allowed → pause account, alert
RATE_LIMIT = "RATE_LIMIT"      # back off, retry later
PUBLISH_LIMIT = "PUBLISH_LIMIT"  # daily publishing quota → skip for today
ACTION_BLOCKED = "ACTION_BLOCKED"  # spam / action block → pause account, alert
NOT_READY = "NOT_READY"        # media still processing
TRANSIENT = "TRANSIENT"        # network / 5xx → retry
COPYRIGHT = "COPYRIGHT"        # copyright match → drop clip, blacklist source
UNKNOWN = "UNKNOWN"

PAUSE_KINDS = {AUTH, PERMISSION, ACTION_BLOCKED}


class IGError(RuntimeError):
    def __init__(self, kind: str, message: str, code=None, subcode=None, http_status=None):
        from redact import redact
        super().__init__(f"{kind}: {redact(message)}")   # never carry tokens/codes in error text
        self.kind = kind
        self.code = code
        self.subcode = subcode
        self.http_status = http_status


def classify(http_status: int, body: dict) -> IGError:
    err = (body or {}).get("error") or {}
    code = err.get("code")
    sub = err.get("error_subcode")
    msg = str(err.get("error_user_msg") or err.get("message") or body)[:500]
    low = msg.lower()
    if code == 190 or err.get("type") == "OAuthException" and code in (102, 463, 467):
        kind = AUTH
    elif sub == 2207042 or "publishing limit" in low:
        kind = PUBLISH_LIMIT
    elif code == 368 or sub == 2207051 or any(w in low for w in ("spam", "action blocked", "restricted",
                                                                    "temporarily blocked")):
        kind = ACTION_BLOCKED
    elif code in (4, 17, 32, 613, 80002) or http_status == 429:
        kind = RATE_LIMIT
    elif code == 10 or (isinstance(code, int) and 200 <= code <= 299):
        kind = PERMISSION
    elif code == 9007 or sub == 2207027 or "not ready" in low:
        kind = NOT_READY
    elif "copyright" in low:
        kind = COPYRIGHT
    elif http_status >= 500 or code in (1, 2):
        kind = TRANSIENT
    else:
        kind = UNKNOWN
    return IGError(kind, msg, code, sub, http_status)
