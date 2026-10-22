"""
instagram/publisher.py — publish one queued reel, and reconcile uncertain ones.

Flow: emergency-stop + guardrail re-check → publishing quota → host video
(R2 temp URL, or resumable upload) → container (caption, thumb_offset,
share_to_feed=true) → poll status_code until FINISHED (copyright check) →
media_publish → posts row.

Double-publish rule: media_publish is called AT MOST ONCE per container. If
its outcome is unclear (network error, 5xx/unclassified error, or the
container says PUBLISHED but the media cannot be found) the post stays
UNKNOWN, the queue item is HELD, no failure is counted and nothing is retried
automatically. reconcile_uncertain() (publish_runner: at start-up and every
10 min) settles these posts by looking at the container and the account's
latest media:
  found                             → PUBLISHED
  not published and older than 2 h  → post FAILED, item back to PENDING (normal retry)
  still unclear after 2 h           → CRITICAL alert + e-mail, item stays HELD
The queue item is PUBLISHING while a publish is in flight, so a crash mid-way
is also left to reconcile (never re-published blindly on restart).

Auto-pause: AUTH / PERMISSION / ACTION_BLOCKED errors, or 3 consecutive failed
publishes → account PAUSED_AUTO, reason stored, its queue HELD, alert + e-mail.
Slow processing (NOT_READY) is not a failure: retry in 30 min, no pause count.
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
from redact import redact
from timeutil import utcnow

log = get_logger("publisher")

RETRY_DELAY = _dt.timedelta(minutes=30)
MAX_ITEM_RETRIES = 3
RECENT_MEDIA_LIMIT = 25
RECONCILE_MIN_AGE = _dt.timedelta(minutes=10)
RECONCILE_GIVE_UP = _dt.timedelta(hours=2)
UNCERTAIN = "UNCERTAIN"


class PublishUncertain(IGError):
    """media_publish may or may not have happened — never retry, let reconcile decide."""

    def __init__(self, message: str):
        super().__init__(UNCERTAIN, message)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()[:120]


def _deprecation_cb(session):
    def cb(msg):
        raise_alert("API_DEPRECATION", f"Instagram API deprecation warning: {msg}", severity="WARNING",
                    session=session)
    return cb


def pause_account(session, acc, reason: str, now=None):
    now = now or utcnow()
    reason = redact(reason)
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


def find_published(client, caption: str, limit: int = RECENT_MEDIA_LIMIT):
    """Look for our caption among the account's latest media (double-publish guard)."""
    want = _norm(caption)
    for m in client.recent_media(limit=limit):
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


def cap_defer_time(session, acc, now):
    """Daily cap hit (the post ran late): retry as soon as the rolling 24 h cap frees up — inside the
    posting window — instead of a whole day later on top of the next day's planned post."""
    from timeutil import to_local, window_day_of, window_for
    t = guardrails.cap_frees_at(session, acc, now)
    if t is None:
        return next_window_start(acc, now)
    t = max(t, now)
    if window_day_of(t, acc.posting_window) is not None:
        return t
    day = to_local(t).date()
    for d in (day - _dt.timedelta(days=1), day, day + _dt.timedelta(days=1)):
        s, _ = window_for(d, acc.posting_window)
        if s >= t:
            return s
    return next_window_start(acc, t)


def publish_item(session, item: UploadQueue, now=None, client_factory=None, host=hosting,
                 sleep=time.sleep, poll_timeout=None) -> str:
    now = now or utcnow()
    wall0 = utcnow()

    def clock():
        """`now` advanced by the real time spent so far (uploads/polling take minutes)."""
        return now + (utcnow() - wall0)

    acc = session.get(OwnedChannels, item.owned_channel_id)
    video = session.get(videos, item.video_id)
    if system_state.emergency_stopped(session):
        return "STOPPED"
    ok, why = guardrails.can_publish(session, acc, now, video)
    if not ok:
        if "8-minute" in why:
            return "DEFER_GAP"
        if "daily cap" in why:
            item.scheduled_for = cap_defer_time(session, acc, now)
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
    session.flush()
    item.post_id = post.id
    item.status = "PUBLISHING"          # a crash from here on is resolved by reconcile, not re-published
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
        post.container_created_at = clock()
        session.commit()
        code = _poll(client, c["id"], settings.IG_POLL_TIMEOUT_S if poll_timeout is None else poll_timeout,
                     sleep)
        if code == "PUBLISHED":                 # published by an earlier attempt: never publish again
            m = find_published(client, video.caption)
            if not m:
                raise PublishUncertain("container PUBLISHED but media not found in recent media")
            media_id = m["id"]
        else:
            media_id = _publish_once(client, c["id"], video.caption)
        _mark_published(session, acc, item, video, post, media_id, client, clock())
        return "PUBLISHED"
    except PublishUncertain as e:
        return _mark_uncertain(session, acc, item, video, post, e)
    except IGError as e:
        return _handle_failure(session, acc, item, video, post, e, now)
    except Exception as e:                  # anything that is not an Instagram API error
        session.rollback()
        return _handle_exception(session, acc, item, video, post, e, now)
    finally:
        if temp:
            host.delete_temp(temp["key"], dry_run=client.dry_run)


def is_r2_config_error(e: Exception) -> bool:
    """Hosting not configured or rejected credentials: an operator problem, not a clip problem."""
    if isinstance(e, hosting.HostingNotConfigured):
        return True
    name = type(e).__name__
    if name in ("NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError"):
        return True
    code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "") if hasattr(e, "response") else ""
    return code in ("InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied", "InvalidToken",
                    "Unauthorized", "NoSuchBucket")


def _handle_exception(session, acc, item, video, post, e: Exception, now) -> str:
    msg = redact(f"{type(e).__name__}: {e}")[:500]
    if post.status != "PENDING":
        # container already exists → its fate is decided by reconcile_uncertain (never re-published blindly)
        log.error("publish interrupted after container (post %s, %s): %s", post.id, post.status, msg)
        return "INTERRUPTED"
    post.status, post.error_message = "FAILED", msg
    item.status = "PENDING"
    if is_r2_config_error(e):
        post.error_kind = "R2_NOT_CONFIGURED"
        item.error_message = "R2 hosting not configured / credentials rejected"
        item.scheduled_for = now + RETRY_DELAY      # no retry_count: not this clip's fault
        session.commit()
        raise_alert("R2_NOT_CONFIGURED", f"Video hosting (R2) unusable — nothing can be published until it is "
                    f"fixed in .env: {msg}", severity="CRITICAL", session=session)
        log.error("R2 not configured: %s", msg)
        return "R2_NOT_CONFIGURED"
    post.error_kind = "EXCEPTION"
    item.retry_count = (item.retry_count or 0) + 1
    item.error_message = msg
    if item.retry_count >= MAX_ITEM_RETRIES:
        item.status = "FAILED"
    else:
        item.scheduled_for = now + RETRY_DELAY
    session.commit()                        # account.consecutive_failures deliberately untouched
    log.error("publish failed before container (post %s): %s", post.id, msg)
    return "RETRY_LOCAL"


def _publish_once(client, container_id, caption):
    """Exactly one media_publish call. Unclear outcome → PublishUncertain (no second call)."""
    try:
        return client.publish(container_id)["id"]
    except IGError as e:
        if e.kind not in (TRANSIENT, UNKNOWN):
            raise                                   # definitive refusal: nothing was published
        log.warning("publish outcome unclear (%s) — looking for the media, no retry", e)
        try:
            m = find_published(client, caption)
        except IGError:
            m = None
        if m:
            return m["id"]
        raise PublishUncertain(f"publish outcome unknown: {e}")


def _mark_uncertain(session, acc, item, video, post, e) -> str:
    post.status = "UNKNOWN"
    post.error_kind = UNCERTAIN
    post.error_message = redact(e)[:500]
    item.status = "HELD"
    item.error_message = "publish outcome uncertain — waiting for reconcile"
    session.commit()
    log.error("publish UNCERTAIN account=%s video=%s post=%s: %s", acc.id, video.id, post.id, e)
    return "UNCERTAIN"


def _mark_published(session, acc, item, video, post, media_id, client, now):
    post.status = "PUBLISHED"
    post.ig_media_id = media_id
    post.published_at = now
    post.error_kind = post.error_message = None
    try:
        post.permalink = client.media(media_id).get("permalink")
    except IGError:
        pass
    if item is not None:
        item.status = "UPLOADED"
        item.uploaded_at = post.published_at
        item.error_message = None
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
    msg = redact(e)
    post.status = "FAILED"
    post.error_kind = e.kind
    post.error_message = msg[:500]
    item.status = "PENDING"             # leaves PUBLISHING; branches below may change it again
    log.error("publish failed account=%s video=%s: %s", acc.id, video.id, msg)
    if e.kind in PAUSE_KINDS:
        session.commit()
        pause_account(session, acc, f"{e.kind}: {msg}", now)
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
    item.retry_count = (item.retry_count or 0) + 1
    item.error_message = msg[:500]
    if item.retry_count >= MAX_ITEM_RETRIES:
        item.status = "FAILED"
    else:
        item.scheduled_for = now + RETRY_DELAY
    if e.kind == NOT_READY:             # Instagram is just slow: not the account's fault
        session.commit()
        return "RETRY_SLOW"
    acc.consecutive_failures = (acc.consecutive_failures or 0) + 1
    session.commit()
    if acc.consecutive_failures >= guardrails.AUTO_PAUSE_AFTER_FAILURES:
        pause_account(session, acc, f"{acc.consecutive_failures} consecutive failed publishes (last: {msg})", now)
        return "PAUSED"
    return "RETRY"


# ═════════════════════════════════════════════════════════════════════════════
# reconcile (publish_runner: at start-up + every 10 minutes)
# ═════════════════════════════════════════════════════════════════════════════

def reconcile_uncertain(session, now=None, client_factory=None) -> dict:
    now = now or utcnow()
    out = {"published": 0, "failed": 0, "alerted": 0, "waiting": 0}
    out["interrupted"] = 0
    posts = (session.query(Post).filter(Post.status.in_(("PENDING", "CONTAINER", "UNKNOWN"))).order_by(Post.id).all())
    for p in posts:
        started = p.container_created_at or p.created_at
        age = now - started
        if age < RECONCILE_MIN_AGE:
            continue
        acc = session.get(OwnedChannels, p.owned_channel_id)
        video = session.get(videos, p.video_id)
        item = session.get(UploadQueue, p.queue_id) if p.queue_id else None
        if p.status == "PENDING":
            # crashed before a container existed → nothing can be on Instagram; plain retry
            if item is None or item.status != "PUBLISHING" or p.ig_container_id:
                continue
            p.status, p.error_kind = "FAILED", "INTERRUPTED_BEFORE_CONTAINER"
            p.error_message = f"publish interrupted before container creation ({age} ago)"
            item.status, item.scheduled_for = "PENDING", now
            item.error_message = "re-queued by reconcile (interrupted before container)"
            session.commit()                # account counters untouched
            log.warning("reconcile: post %s interrupted before container → item %s re-queued", p.id, item.id)
            out["interrupted"] += 1
            continue
        client = (client_factory or client_for)(acc)
        code, media = None, None
        try:
            if p.ig_container_id:
                code = client.container_status(p.ig_container_id).get("status_code")
            media = find_published(client, p.caption)
        except IGError as e:
            log.warning("reconcile post %s: lookup failed: %s", p.id, e)
        if media:
            _mark_published(session, acc, item, video, p, media["id"], client, now)
            log.warning("reconcile: post %s was actually published (media %s)", p.id, media["id"])
            out["published"] += 1
            continue
        if age < RECONCILE_GIVE_UP:
            if item is not None and item.status == "PUBLISHING":
                item.status = "HELD"
                item.error_message = "publish interrupted — waiting for reconcile"
                session.commit()
            out["waiting"] += 1
            continue
        if code is not None and code != "PUBLISHED":
            # Instagram says the container was never published → safe to retry normally
            p.status = "FAILED"
            p.error_kind = "RECONCILED_NOT_PUBLISHED"
            p.error_message = f"container {code}; media not found after {age}"
            if item is not None:
                item.status = "PENDING"
                item.scheduled_for = now
                item.error_message = "re-queued by reconcile (not published)"
            session.commit()
            log.info("reconcile: post %s not published (container %s) → item re-queued", p.id, code)
            out["failed"] += 1
            continue
        if item is not None and item.status != "HELD":
            item.status = "HELD"
        p.status = "UNKNOWN"
        session.commit()
        raise_alert("PUBLISH_UNCERTAIN",
                    f"Post {p.id} (account {acc.ig_username or acc.id}, video {video.id}) still unclear after "
                    f"{RECONCILE_GIVE_UP}: container={code or 'unknown'}, media not found. Check the account "
                    "manually; the queue item stays HELD.",
                    severity="CRITICAL", account_id=acc.id, session=session)
        out["alerted"] += 1
    return out


def _is_trial(session, video) -> bool:
    if not video.source_id:
        return False
    src = session.get(Source, video.source_id)
    return bool(src and src.status == "TRIAL")


def _slot_hour(dt_utc, acc):
    from timeutil import slot_key_of
    return slot_key_of(dt_utc, acc.posting_window) if dt_utc else None
