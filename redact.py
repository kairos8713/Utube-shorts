"""
redact.py — strip secrets from any text before it is logged, stored or e-mailed.

Masks: access_token / client_secret / refresh-style params in URLs and JSON,
OAuth authorization codes (`code=` with a long value), Authorization headers,
raw Instagram tokens (IGAA…/IGQV…), and every secret registered at runtime
(decrypted tokens, IG_APP_SECRET, SMTP password).

Used by: IGError (all IG error texts), the HTTP transport, OAuth flow, token
refresh, the log formatter (every log line incl. tracebacks), alerts and e-mail.
"""
import re
import threading

MASK = "***"

_PARAM_RE = re.compile(
    r"((?:access_token|client_secret|refresh_token|fb_exchange_token|input_token|appsecret_proof"
    r"|X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token)"
    r"(?:%3D|=|\"\s*:\s*\"|'\s*:\s*'))([^&\s\"',;)}]+)", re.I)
_CODE_RE = re.compile(r"((?<![\w-])code(?:%3D|=|\"\s*:\s*\"))([A-Za-z0-9_\-#.%]{16,})", re.I)
_AUTH_RE = re.compile(r"((?:Authorization\W{1,4})?(?:OAuth|Bearer)\s+)[A-Za-z0-9._\-~+/=]{12,}", re.I)
_IG_TOKEN_RE = re.compile(r"\bIG(?:AA|QV|QW)[A-Za-z0-9_\-]{16,}")

_secrets = set()
_lock = threading.Lock()


def register_secret(value):
    """Mask this exact value everywhere from now on (tokens are registered when decrypted)."""
    if value and isinstance(value, str) and len(value) >= 8:
        with _lock:
            _secrets.add(value)


def _known_secrets():
    out = set(_secrets)
    try:
        from config import settings
        for name in ("IG_APP_SECRET", "SMTP_PASSWORD", "R2_SECRET_ACCESS_KEY", "FERNET_KEY", "PANEL_SECRET_KEY"):
            v = getattr(settings, name)
            if v and len(v) >= 8:
                out.add(v)
    except Exception:
        pass
    return out


def redact(text) -> str:
    if text is None:
        return text
    s = str(text)
    for secret in sorted(_known_secrets(), key=len, reverse=True):
        if secret in s:
            s = s.replace(secret, MASK)
    s = _PARAM_RE.sub(lambda m: m.group(1) + MASK, s)
    s = _CODE_RE.sub(lambda m: m.group(1) + MASK, s)
    s = _AUTH_RE.sub(lambda m: m.group(1) + MASK, s)
    s = _IG_TOKEN_RE.sub(MASK, s)
    return s
