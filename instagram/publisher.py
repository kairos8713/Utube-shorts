"""
instagram/publisher.py — publish one queued reel.

Flow: emergency-stop + guardrail re-check → publishing quota → host video
(R2 temp URL, or resumable upload) → container (caption, thumb_offset,
share_to_feed=true) → poll status_code until FINISHED (copyright check) →
media_publish → posts row.

Double-publish guard: if media_publish gives no usable answer (network error,
5xx), we do NOT blindly retry — first we look at the container status and the
account's latest media; only if the reel is not there do we retry once.

Auto-pause: AUTH / PERMISSION / ACTION_BLOCKED errors, or 3 consecutive failed
publishes → account PAUSED_AUTO, reason stored, its queue HELD, alert + e-mail.
Daily publishing quota reached → this item moves to the next day only.
"""
import datetime as _dt
import os
import re
import time

import guardrails
import system_state
from alerts import raise_alert
from config import settings
from instagram import hosting
from instagram.client import client_for
from instagram.errors import (COPYRIGHT, IGError, NOT_READY, PAUSE_KINDS, PUBLISH_LIMIT, TRANSIENT, UNKNOWN)
from log_setup import get_logger
from models import OwnedChannels, Post, Source, UploadQueue, videos
from timeutil import utcnow

log = get_logger("publisher")

RETRY_DELAY = _dt.timedelta(minutes=30)
MAX_ITEM_RETRIES = 3


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()[:120]


def _deprecation_cb(session):
    def cb(msg):
        raise_alert("API_DEPRECATION", f"Instagram API deprecation warning: {msg}", severity="WARNING",
                    session=session)
    return cb


def pause_account(session, acc, reason: str, now=None):
    now = now or utcnow()
    acc.status = "PAUSED_AUTO"
    acc.pause_reason = reason[:300]
    acc.paused_at = now
    held = (session.query(UploadQueue).filter_by(owned_channel_id=acc.id, status="PENDING")
            .update({"status": "HELD"}))
    session.commit()
    raise_alert("ACCOUNT_AUTO_PAUSED", f"Account {acc.ig_username or acc.id} auto-paused: {reason}. "
                f"{held} queued reels held until you resume it in the panel.", severity="CRITICAL",
                account_id=acc.id, session=session)
    log.error("account %s PAUSED_AUTO: %s", acc.id, reason)


def find_published(client, caption: str):
    """Look for our caption among the account's latest media (double-publish guard)."""
    want = _norm(caption)
    for m in client.recent_media(limit=10):
        if _norm(m.get("caption")) == want:
            return m
    return None


def _poll(client, container_id, timeout_s, sleep, interval=30):
    deadline = time.time() + timeout_s
    while True:
        st = client.container_status(container_id)
        code = st.get("status_code")
        cc = st.get("copyright_check_status") or {}
        if cc.get("matches_found") in (True, "true"):
            raise IGError(COPYRIGHT, f"copyright match on container {container_id}")
        if code in ("FINISHED", "PUBLISHED"):
            return code
        if code in ("ERROR", "EXPIRED"):
            raise IGError(UNKNOWN if code == "ERROR" else TRANSIENT, f"container {code}: {st.get('status')}")
        if time.time() >= deadline:
            raise IGError(NOT_READY, f"container still {code} after {timeout_s}s")
        sleep(interval)
        interval = min(interval * 2, 60)


def next_window_start(acc, now):
    from timeutil import to_local, window_for
    day = to_local(now).date() + _dt.timedelta(days=1)
    s, _ = window_for(day, acc.posting_window)
    return s


def publish_item(session, item: UploadQueue, now=None, client_factory=None, host=hosting,
                 sleep=time.sleep, poll_timeout=None) -> str:
    now = now or utcnow()
    acc = session.get(OwnedChannels, item.owned_channel_id)
    video = session.get(videos, item.video_id)
    if system_state.emergency_stopped(session):
        return "STOPPED"
    ok, why = guardrails.can_publish(session, acc, now, video)
    if not ok:
        if "8-minute" in why:
            return "DEFER_GAP"
        if "daily cap" in why:
            item.scheduled_for = next_window_start(acc, now)
            session.commit()
            return "DEFER_CAP"
        if why.startswith("account status") or "warm-up" in why or "not connected" in why or "expired" in why:
            item.status = "HELD"
            item.error_message = why
            session.commit()
            return "HELD"
        item.status = "SKIPPED"
        item.error_message = why
        session.commit()
        log.warning("queue item %s skipped: %s", item.id, why)
        return "SKIPPED"
    if not video.file_path or not os.path.exists(video.file_path):
        item.status = "FAILED"
        item.error_message = "rendered file missing"
        session.commit()
        return "FAILED"

    client = (client_factory or client_for)(acc, on_deprecation=_deprecation_cb(session))
    post = Post(owned_channel_id=acc.id, video_id=video.id, queue_id=item.id, status="PENDING",
                caption=video.caption, scheduled_for=item.scheduled_for, slot_source=item.slot_source,
                slot_hour=_slot_hour(item.scheduled_for, acc), phash=video.phash, source_id=video.source_id,
                source_url=video.source_url, segment_start=video.segment_start, segment_end=video.segment_end,
                is_trial=_is_trial(session, video), dry_run=client.dry_run, created_at=now, attempts=1)
    session.add(post)
    item.post_id = None
    session.flush()
    item.post_id = post.id
    session.commit()

    temp = None
    try:
        lim = client.publishing_limit()
        if (lim.get("quota_usage") or 0) >= ((lim.get("config") or {}).get("quota_total") or 50):
            raise IGError(PUBLISH_LIMIT, "content_publishing_limit reached")
        if settings.IG_UPLOAD_METHOD == "resumable":
            c = client.create_reel_container(video.caption, resumable=True, thumb_offset_ms=video.cover_offset_ms)
            client.upload_resumable(c["uri"], video.file_path)
        else:
            temp = host.upload_temp(video.file_path, dry_run=client.dry_run)
            c = client.create_reel_container(video.caption, video_url=temp["url"],
                                             thumb_offset_ms=video.cover_offset_ms)
        post.ig_container_id = c["id"]
        post.status = "CONTAINER"
        post.container_created_at = max(now, utcnow())
        session.commit()
        code = _poll(client, c["id"], poll_timeout or settings.IG_POLL_TIMEOUT_S, sleep)
        media_id = None
        if code == "PUBLISHED":                         # someone (a previous run) already published it
            m = find_published(client, video.caption)
            media_id = m and m.get("id")
        if not media_id:
            media_id = _publish_guarded(client, post, c["id"], video.caption)
        _mark_published(session, acc, item, video, post, media_id, client, now)
        return "PUBLISHED"
    except IGError as e:
        return _handle_failure(session, acc, item, video, post, e, now)
    finally:
        if temp:
            host.delete_temp(temp["key"], dry_run=client.dry_run)


def _publish_guarded(client, post, container_id, caption):
    try:
        return client.publish(container_id)["id"]
    except IGError as e:
        if e.kind not in (TRANSIENT, UNKNOWN):
            raise
        log.warning("publish response unusable (%s) — checking before any retry", e)
        post.status = "UNKNOWN"
        st = client.container_status(container_id)
        m = find_published(client, caption)
        if m:
            return m["id"]
        if st.get("status_code") == "PUBLISHED":
            raise IGError(UNKNOWN, "container PUBLISHED but media not found in recent media")
        return client.publish(container_id)["id"]       # exactly one retry


def _mark_published(session, acc, item, video, post, media_id, client, now):
    post.status = "PUBLISHED"
    post.ig_media_id = media_id
    post.published_at = max(now, utcnow())
    try:
        post.permalink = client.media(media_id).get("permalink")
    except IGError:
        pass
    item.status = "UPLOADED"
    item.uploaded_at = post.published_at
    video.is_used = True
    video.ig_reel_id = media_id
    acc.consecutive_failures = 0
    acc.last_uploaded_at = post.published_at
    if video.source_id:
        src = session.get(Source, video.source_id)
        if src:
            src.clips_published = (src.clips_published or 0) + 1
    session.commit()
    log.info("published video %s on account %s → media %s%s", video.id, acc.id, media_id,
             " [DRY_RUN]" if client.dry_run else "")
    try:
        import maintenance
        maintenance.cleanup_after_publish(session, video)
    except ImportError:
        pass


def _handle_failure(session, acc, item, video, post, e: IGError, now) -> str:
    post.status = "FAILED"
    post.error_kind = e.kind
    post.error_message = str(e)[:500]
    log.error("publish failed account=%s video=%s: %s", acc.id, video.id, e)
    if e.kind in PAUSE_KINDS:
        session.commit()
        pause_account(session, acc, f"{e.kind}: {e}", now)
        return "PAUSED"
    if e.kind == PUBLISH_LIMIT:
        post.status = "SKIPPED"
        item.scheduled_for = next_window_start(acc, now)
        session.commit()
        return "DEFER_LIMIT"
    if e.kind == COPYRIGHT:
        item.status = "FAILED"
        item.error_message = "copyright match"
        session.commit()
        try:
            import sources_manager
            sources_manager.blacklist_source(session, video.source_id, "copyright match on publish")
        except ImportError:
            pass
        raise_alert("COPYRIGHT_MATCH", f"Copyright match on video {video.id} (source {video.source_url}); "
                    "source blacklisted", severity="WARNING", account_id=acc.id, session=session)
        return "COPYRIGHT"
    acc.consecutive_failures = (acc.consecutive_failures or 0) + 1
    item.retry_count = (item.retry_count or 0) + 1
    item.error_message = str(e)[:500]
    if item.retry_count >= MAX_ITEM_RETRIES:
        item.status = "FAILED"
    else:
        item.scheduled_for = now + RETRY_DELAY
    session.commit()
    if acc.consecutive_failures >= guardrails.AUTO_PAUSE_AFTER_FAILURES:
        pause_account(session, acc, f"{acc.consecutive_failures} consecutive failed publishes (last: {e})", now)
        return "PAUSED"
    return "RETRY"


def _is_trial(session, video) -> bool:
    if not video.source_id:
        return False
    src = session.get(Source, video.source_id)
    return bool(src and src.status == "TRIAL")


def _slot_hour(dt_utc, acc):
    from timeutil import slot_key_of
    return slot_key_of(dt_utc, acc.posting_window) if dt_utc else None
