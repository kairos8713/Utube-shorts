"""
instagram/client.py — Instagram API with Instagram Login (graph.instagram.com).

* API version pinned (IG_API_VERSION, see DECISIONS D-020).
* Errors classified (instagram/errors.py).
* DRY_RUN (default true): NO request leaves the machine; the intended request
  is logged and a deterministic fake response returned.
* Deprecation/usage warnings in response headers are reported via
  `on_deprecation` (wired to an alert + e-mail by the services).
* By design this client has NO methods to delete media/comments, revoke
  tokens, or touch passwords / account settings.
"""
import hashlib
import json
import os
import time
from typing import Callable, Optional

from config import settings
from instagram.errors import IGError, TRANSIENT, classify
from log_setup import get_logger

log = get_logger("ig_client")

GRAPH_HOST = "https://graph.instagram.com"
RUPLOAD_HOST = "https://rupload.facebook.com/ig-api-upload"

REELS_METRICS = ("views", "reach", "likes", "comments", "shares", "saved", "total_interactions",
                 "ig_reels_avg_watch_time")


def _requests_transport(method, url, params=None, data=None, headers=None, body=None, timeout=60):
    import requests
    try:
        r = requests.request(method, url, params=params, data=body if body is not None else data,
                             headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise IGError(TRANSIENT, f"network: {e}")
    try:
        payload = r.json()
    except ValueError:
        payload = {"raw": r.text[:500]}
    return r.status_code, payload, dict(r.headers)


class IGClient:
    def __init__(self, access_token: str, ig_user_id: str, *, dry_run: bool = None,
                 transport: Callable = None, version: str = None,
                 on_deprecation: Callable[[str], None] = None, sleep=time.sleep):
        self.token = access_token
        self.user_id = ig_user_id
        self.dry_run = settings.DRY_RUN if dry_run is None else dry_run
        self.transport = transport or _requests_transport
        self.version = version or settings.IG_API_VERSION
        self.on_deprecation = on_deprecation
        self.sleep = sleep
        self.dry_log = []          # what would have been sent (DRY_RUN)

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        return f"{GRAPH_HOST}/{self.version}/{path.lstrip('/')}"

    def _check_headers(self, headers: dict):
        for k, v in (headers or {}).items():
            lk = k.lower()
            if "deprecat" in lk or lk in ("sunset", "x-fb-api-version-warning", "x-ad-api-version-warning") \
                    or ("warning" in lk and "deprecat" in str(v).lower()):
                msg = f"{k}: {v}"
                log.warning("API deprecation signal %s", msg)
                if self.on_deprecation:
                    self.on_deprecation(msg)

    def _request(self, method: str, path: str, params: dict = None, retries: int = 2,
                 url: str = None, headers: dict = None, body=None):
        params = dict(params or {})
        if self.dry_run:
            safe = {k: ("<token>" if k == "access_token" else v) for k, v in params.items()}
            entry = {"method": method, "url": url or self._url(path), "params": safe,
                     "body_bytes": len(body) if isinstance(body, (bytes, bytearray)) else None}
            self.dry_log.append(entry)
            log.info("[DRY_RUN] %s %s %s", method, entry["url"], json.dumps(safe, default=str)[:400])
            return self._fake_response(method, path, params)
        if not url:
            params["access_token"] = self.token
        last = None
        for attempt in range(retries + 1):
            try:
                status, payload, hdrs = self.transport(method, url or self._url(path), params=params,
                                                       headers=headers, body=body, timeout=120)
            except IGError as e:
                last = e
                self.sleep(5 * (attempt + 1))
                continue
            self._check_headers(hdrs)
            if status < 400 and not (isinstance(payload, dict) and payload.get("error")):
                return payload
            err = classify(status, payload)
            if err.kind == TRANSIENT and attempt < retries:
                last = err
                self.sleep(5 * (attempt + 1))
                continue
            raise err
        raise last

    def _fake_response(self, method, path, params):
        h = hashlib.sha1(f"{method}{path}{sorted(params.items())}".encode()).hexdigest()[:12]
        if path.endswith("/media") and method == "POST":
            return {"id": f"dry_container_{h}", "uri": f"{RUPLOAD_HOST}/{self.version}/dry_container_{h}"}
        if path.endswith("/media_publish"):
            return {"id": f"dry_media_{h}"}
        if "fields" in params and "status_code" in params["fields"]:
            return {"id": path, "status_code": "FINISHED",
                    "copyright_check_status": {"status": "completed", "matches_found": False}}
        if path.endswith("/content_publishing_limit"):
            return {"data": [{"quota_usage": 0, "config": {"quota_total": 50, "quota_duration": 86400}}]}
        if path.endswith("/comments"):
            return {"data": []}
        if path.endswith("/replies"):
            return {"id": f"dry_reply_{h}"}
        if path.endswith("/insights"):
            return {"data": []}
        if path.endswith("/media") and method == "GET":
            return {"data": []}
        return {"id": path, "success": True}

    # ── account ──────────────────────────────────────────────────────────────
    def me(self, fields: str = "user_id,username,account_type,followers_count,media_count") -> dict:
        return self._request("GET", "me", {"fields": fields})

    def publishing_limit(self) -> dict:
        data = self._request("GET", f"{self.user_id}/content_publishing_limit",
                             {"fields": "quota_usage,config"}).get("data") or [{}]
        return data[0]

    # ── publishing ───────────────────────────────────────────────────────────
    def create_reel_container(self, caption: str, video_url: Optional[str] = None, resumable: bool = False,
                              thumb_offset_ms: Optional[int] = None, cover_url: Optional[str] = None,
                              share_to_feed: bool = True) -> dict:
        params = {"media_type": "REELS", "caption": caption, "share_to_feed": "true" if share_to_feed else "false"}
        if resumable:
            params["upload_type"] = "resumable"
        elif video_url:
            params["video_url"] = video_url
        else:
            raise ValueError("video_url or resumable=True required")
        if cover_url:
            params["cover_url"] = cover_url
        elif thumb_offset_ms is not None:
            params["thumb_offset"] = int(thumb_offset_ms)
        return self._request("POST", f"{self.user_id}/media", params)

    def upload_resumable(self, upload_uri: str, file_path: str) -> dict:
        """Docs: resumable upload is Facebook-Login-only (DECISIONS D-020)."""
        size = os.path.getsize(file_path)
        headers = {"Authorization": f"OAuth {self.token}", "offset": "0", "file_size": str(size)}
        if self.dry_run:
            return self._request("POST", "rupload", {"file_size": size}, url=upload_uri)
        with open(file_path, "rb") as f:
            data = f.read()
        return self._request("POST", "rupload", None, url=upload_uri, headers=headers, body=data)

    def container_status(self, container_id: str) -> dict:
        return self._request("GET", container_id, {"fields": "status_code,status,copyright_check_status"})

    def publish(self, container_id: str) -> dict:
        return self._request("POST", f"{self.user_id}/media_publish", {"creation_id": container_id}, retries=0)

    def recent_media(self, limit: int = 10) -> list:
        return self._request("GET", f"{self.user_id}/media",
                             {"fields": "id,caption,timestamp,permalink", "limit": limit}).get("data", [])

    def media(self, media_id: str, fields: str = "id,caption,timestamp,permalink") -> dict:
        return self._request("GET", media_id, {"fields": fields})

    # ── comments ─────────────────────────────────────────────────────────────
    def comments(self, media_id: str, limit: int = 50) -> list:
        return self._request("GET", f"{media_id}/comments",
                             {"fields": "id,text,username,timestamp", "limit": limit}).get("data", [])

    def reply(self, comment_id: str, message: str) -> dict:
        return self._request("POST", f"{comment_id}/replies", {"message": message}, retries=0)

    def hide_comment(self, comment_id: str, hide: bool = True) -> dict:
        return self._request("POST", comment_id, {"hide": "true" if hide else "false"}, retries=0)

    # ── insights ─────────────────────────────────────────────────────────────
    def media_insights(self, media_id: str, metrics=REELS_METRICS) -> dict:
        data = self._request("GET", f"{media_id}/insights", {"metric": ",".join(metrics)}).get("data", [])
        out = {}
        for row in data:
            vals = row.get("values") or [{}]
            out[row.get("name")] = row.get("total_value", {}).get("value", vals[0].get("value"))
        return out

    def account_insights(self, metric: str, **params) -> list:
        return self._request("GET", f"{self.user_id}/insights", {"metric": metric, **params}).get("data", [])


def client_for(account, **kw) -> IGClient:
    from crypto_utils import decrypt
    return IGClient(decrypt(account.ig_access_token) if account.ig_access_token else "", account.ig_user_id or "",
                    **kw)
