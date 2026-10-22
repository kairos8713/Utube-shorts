"""
guardrails.py — hard safety limits. Qwen CANNOT change these.

There is deliberately no setter, no DB override and no .env override for any
constant below; the panel shows them read-only (describe()). brain/ proposals
are validated against these checks before anything is applied, and every
external action re-checks them immediately before acting.
"""
import datetime as _dt
from typing import Optional, Tuple

from system_state import emergency_stopped
from timeutil import to_local, utcnow, window_for

# ── constants ────────────────────────────────────────────────────────────────
DAILY_CAP_START = 1                    # posts / account / posting-window day for new accounts
DAILY_CAP_ABSOLUTE_MAX = 2             # never more, whatever Qwen proposes
# least time between two posts of the SAME account, by its daily cap (Emre 2026-10-23: replaces the
# rolling 24 h count, which clashed with a random minute inside the slot)
MIN_GAP_SAME_ACCOUNT = {1: _dt.timedelta(hours=14), 2: _dt.timedelta(hours=6)}
CAP_2_MIN_ACTIVE_DAYS = 14             # cap 2 only after ≥14 healthy active days
MIN_GAP_BETWEEN_POSTS = _dt.timedelta(minutes=8)    # system-wide, across accounts
WARMUP = _dt.timedelta(days=2)         # ig_setup_completed_at + 2 days before the first post
AUTO_PAUSE_AFTER_FAILURES = 3          # consecutive failed publishes → PAUSED_AUTO
REPLIES_PER_ACCOUNT_PER_HOUR = 5
REPLIES_PER_ACCOUNT_PER_DAY = 30
HIDES_PER_ACCOUNT_PER_DAY = 30
REPLY_MAX_COMMENT_AGE_AFTER_POST = _dt.timedelta(hours=48)
REPLY_DELAY_MINUTES = (40, 70)
EXPLORE_SHARE = 0.25                   # share of posts placed in non-Qwen slots
HEALTHY_LOOKBACK = _dt.timedelta(days=14)

ACCOUNT_STATUSES = ("ACTIVE", "PAUSED", "PAUSED_AUTO", "WARMUP", "DISCONNECTED")


def describe() -> list:
    return [
        ("DAILY_CAP_START", DAILY_CAP_START, "Yeni hesap: yayın-penceresi günü (Warsaw, pencerenin başladığı tarih) başına en fazla paylaşım"),
        ("DAILY_CAP_ABSOLUTE_MAX", DAILY_CAP_ABSOLUTE_MAX, "Mutlak üst sınır (pencere günü başına)"),
        ("MIN_GAP_SAME_ACCOUNT", "cap 1: 14 saat · cap 2: 6 saat", "Aynı hesabın iki paylaşımı arası en az süre"),
        ("CAP_2_MIN_ACTIVE_DAYS", CAP_2_MIN_ACTIVE_DAYS, "2/gün için en az aktif + sağlıklı gün"),
        ("MIN_GAP_BETWEEN_POSTS", "8 dk", "Sistem genelinde iki paylaşım arası en az süre"),
        ("WARMUP", "2 gün", "ig_setup_completed_at + 2 gün dolmadan paylaşım yok"),
        ("AUTO_PAUSE_AFTER_FAILURES", AUTO_PAUSE_AFTER_FAILURES, "Ardışık başarısız yayın → PAUSED_AUTO"),
        ("REPLIES_PER_ACCOUNT_PER_HOUR", REPLIES_PER_ACCOUNT_PER_HOUR, "Yorum yanıtı / hesap / saat"),
        ("REPLIES_PER_ACCOUNT_PER_DAY", REPLIES_PER_ACCOUNT_PER_DAY, "Yorum yanıtı / hesap / gün"),
        ("HIDES_PER_ACCOUNT_PER_DAY", HIDES_PER_ACCOUNT_PER_DAY, "Yorum gizleme / hesap / gün"),
        ("REPLY_MAX_COMMENT_AGE_AFTER_POST", "48 saat", "Bundan sonra gelen yorumlar yanıtlanmaz"),
        ("REPLY_DELAY_MINUTES", "40–70 dk", "Her yanıt için rastgele gecikme"),
        ("EXPLORE_SHARE", "25%", "Qwen'in seçmediği dilimlere giden paylaşım oranı"),
        ("EMERGENCY_STOP", "panel", "Açıkken hiçbir dış eylem yapılmaz"),
    ]


# ── account state ────────────────────────────────────────────────────────────

def warmup_ok(account, now: _dt.datetime = None) -> Tuple[bool, str]:
    now = now or utcnow()
    if not account.ig_setup_completed_at:
        return False, "ig_setup_completed_at not set — no uploads until Emre enters it"
    ready = account.ig_setup_completed_at + WARMUP
    if now < ready:
        return False, f"warm-up until {ready:%Y-%m-%d %H:%M} UTC"
    return True, ""


def refresh_account_status(account, now: _dt.datetime = None) -> bool:
    """WARMUP → ACTIVE once the warm-up gate passes (code only; paused accounts untouched)."""
    now = now or utcnow()
    if (account.status or "WARMUP") == "WARMUP" and account.ig_access_token and warmup_ok(account, now)[0]:
        account.status = "ACTIVE"
        account.activated_at = account.activated_at or now
        if not account.daily_post_cap:
            account.daily_post_cap = DAILY_CAP_START
        return True
    return False


def active_days(account, now: _dt.datetime = None) -> float:
    now = now or utcnow()
    since = account.activated_at or (account.ig_setup_completed_at + WARMUP if account.ig_setup_completed_at else None)
    return max(0.0, (now - since).total_seconds() / 86400.0) if since else 0.0


def account_healthy(session, account, now: _dt.datetime = None) -> Tuple[bool, str]:
    from models import Alert
    now = now or utcnow()
    if account.status != "ACTIVE":
        return False, f"status {account.status}"
    if (account.consecutive_failures or 0) > 0:
        return False, "recent publish failures"
    if account.paused_at and now - account.paused_at < HEALTHY_LOOKBACK:
        return False, "paused within the last 14 days"
    crit = session.query(Alert).filter_by(owned_channel_id=account.id, resolved=False, severity="CRITICAL").count()
    if crit:
        return False, "unresolved critical alerts"
    try:
        from analytics import early_warning_active
        if early_warning_active(session, account, now):
            return False, "early-warning: reach dropped"
    except ImportError:
        pass
    return True, ""


def max_cap_allowed(session, account, now: _dt.datetime = None) -> int:
    now = now or utcnow()
    if active_days(account, now) >= CAP_2_MIN_ACTIVE_DAYS and account_healthy(session, account, now)[0]:
        return DAILY_CAP_ABSOLUTE_MAX
    return DAILY_CAP_START


def effective_cap(session, account, now: _dt.datetime = None) -> int:
    cap = account.daily_post_cap or DAILY_CAP_START
    return max(0, min(cap, max_cap_allowed(session, account, now), DAILY_CAP_ABSOLUTE_MAX))


def validate_cap_change(session, account, new_cap, now: _dt.datetime = None) -> list:
    if not isinstance(new_cap, int) or isinstance(new_cap, bool):
        return ["daily_post_cap must be an integer"]
    if new_cap < 1:
        return ["cap below 1 is not allowed — pause the account instead"]
    if new_cap > DAILY_CAP_ABSOLUTE_MAX:
        return [f"cap {new_cap} exceeds absolute max {DAILY_CAP_ABSOLUTE_MAX}"]
    if new_cap > max_cap_allowed(session, account, now):
        return [f"cap {new_cap} needs ≥{CAP_2_MIN_ACTIVE_DAYS} active days and a healthy account"]
    return []


# ── publishing ───────────────────────────────────────────────────────────────

CAP_COUNTED_STATUSES = ("PUBLISHED", "CONTAINER", "UNKNOWN")


def min_gap_same_account(cap: int) -> _dt.timedelta:
    """Least time between two posts of one account for its daily cap."""
    return MIN_GAP_SAME_ACCOUNT.get(cap, MIN_GAP_SAME_ACCOUNT[DAILY_CAP_ABSOLUTE_MAX])


def cap_day(account, now: _dt.datetime = None) -> _dt.date:
    """The posting-window day the daily cap counts `now` in: the window that started most recently
    (15:00–05:00 crosses midnight, so the day is the window's start date; 05:00–15:00 still belongs to
    the window that just ended)."""
    now = now or utcnow()
    loc = to_local(now).date()
    return max(d for d in (loc - _dt.timedelta(days=1), loc) if window_for(d, account.posting_window)[0] <= now)


def cap_day_bounds(account, day: _dt.date):
    """[start of that day's window, start of the next day's window) in UTC."""
    return window_for(day, account.posting_window)[0], window_for(day + _dt.timedelta(days=1),
                                                                    account.posting_window)[0]


def _counted_posts(session, account_id: int, since: _dt.datetime, until: _dt.datetime = None):
    from models import Post
    q = session.query(Post.created_at).filter(Post.owned_channel_id == account_id,
                                              Post.status.in_(CAP_COUNTED_STATUSES), Post.created_at >= since)
    if until is not None:
        q = q.filter(Post.created_at < until)
    return sorted(r[0] for r in q.all() if r[0])


def posts_in_cap_day(session, account, now: _dt.datetime = None) -> int:
    lo, hi = cap_day_bounds(account, cap_day(account, now))
    return len(_counted_posts(session, account.id, lo, hi))


def last_account_post_time(session, account_id: int, now: _dt.datetime = None) -> Optional[_dt.datetime]:
    now = now or utcnow()
    times = _counted_posts(session, account_id, now - _dt.timedelta(days=2))
    return times[-1] if times else None


def next_allowed_post_time(session, account, now: _dt.datetime = None) -> Optional[_dt.datetime]:
    """Earliest time ≥ now at which the daily cap AND the same-account gap pass (None with a zero cap).
    May fall outside the posting window — the caller moves it into the next window."""
    now = now or utcnow()
    cap = effective_cap(session, account, now)
    if cap <= 0:
        return None
    t = now
    if posts_in_cap_day(session, account, now) >= cap:
        t = cap_day_bounds(account, cap_day(account, now))[1]          # next window day
    last = last_account_post_time(session, account.id, now)
    if last is not None:
        t = max(t, last + min_gap_same_account(cap))
    return t


def last_global_post_time(session) -> Optional[_dt.datetime]:
    from models import Post
    row = (session.query(Post).filter(Post.status.in_(("PUBLISHED", "CONTAINER", "UNKNOWN")))
           .order_by(Post.created_at.desc()).first())
    if not row:
        return None
    return row.published_at or row.container_created_at or row.created_at


def can_publish(session, account, now: _dt.datetime = None, video=None) -> Tuple[bool, str]:
    now = now or utcnow()
    if emergency_stopped(session):
        return False, "emergency stop active"
    refresh_account_status(account, now)
    if account.status != "ACTIVE":
        return False, f"account status {account.status}"
    ok, why = warmup_ok(account, now)
    if not ok:
        return False, why
    if not account.ig_access_token or not account.ig_user_id:
        return False, "account not connected (no token)"
    if account.ig_token_expires_at and account.ig_token_expires_at <= now:
        return False, "access token expired"
    cap = effective_cap(session, account, now)
    used = posts_in_cap_day(session, account, now)
    if used >= cap:
        return False, f"daily cap reached ({used}/{cap} in window day {cap_day(account, now)})"
    last = last_account_post_time(session, account.id, now)
    gap = min_gap_same_account(cap)
    if last is not None and now - last < gap:
        return False, (f"same-account gap: last post {(now - last).total_seconds() / 3600:.1f} h ago "
                       f"(min {gap.total_seconds() / 3600:.0f} h at cap {cap})")
    last = last_global_post_time(session)
    if last and now - last < MIN_GAP_BETWEEN_POSTS:
        return False, f"global 8-minute gap (last post {int((now - last).total_seconds())}s ago)"
    if video is not None:
        reason = cross_account_duplicate(session, account.id, video)
        if reason:
            return False, reason
    return True, ""


def cross_account_duplicate(session, account_id: int, video) -> Optional[str]:
    """A clip/segment already posted (or posting) on ANY account blocks this post."""
    from dedup import OVERLAP_TOLERANCE_S, phash_match
    from models import Post
    rows = (session.query(Post).filter(Post.status.in_(("PUBLISHED", "CONTAINER", "UNKNOWN")),
                                       Post.video_id != video.id).all())
    for p in rows:
        if video.source_url and p.source_url == video.source_url and p.segment_start is not None \
                and video.segment_start is not None \
                and video.segment_start < p.segment_end + OVERLAP_TOLERANCE_S \
                and p.segment_start < video.segment_end + OVERLAP_TOLERANCE_S:
            return f"segment already posted (post #{p.id}, account {p.owned_channel_id})"
        if phash_match(video.phash, p.phash):
            return f"perceptual duplicate of post #{p.id}"
    already = session.query(Post).filter(Post.video_id == video.id,
                                         Post.status.in_(("PUBLISHED", "CONTAINER", "UNKNOWN"))).first()
    if already:
        return f"video already posted (post #{already.id})"
    return None


# ── comments ─────────────────────────────────────────────────────────────────

def _replies_since(session, account_id: int, since: _dt.datetime) -> int:
    from models import CommentReply
    return (session.query(CommentReply)
            .filter(CommentReply.owned_channel_id == account_id, CommentReply.status == "SENT",
                    CommentReply.sent_at >= since).count())


def can_reply(session, account, post, comment, now: _dt.datetime = None) -> Tuple[bool, str]:
    now = now or utcnow()
    if emergency_stopped(session):
        return False, "emergency stop active"
    if account.status != "ACTIVE":
        return False, f"account status {account.status}"
    if post.published_at and comment.commented_at \
            and comment.commented_at - post.published_at > REPLY_MAX_COMMENT_AGE_AFTER_POST:
        return False, "comment arrived more than 48 h after the post"
    if _replies_since(session, account.id, now - _dt.timedelta(hours=1)) >= REPLIES_PER_ACCOUNT_PER_HOUR:
        return False, "hourly reply cap"
    if _replies_since(session, account.id, now - _dt.timedelta(hours=24)) >= REPLIES_PER_ACCOUNT_PER_DAY:
        return False, "daily reply cap"
    return True, ""


def can_hide(session, account, now: _dt.datetime = None) -> Tuple[bool, str]:
    from models import Comment
    now = now or utcnow()
    if emergency_stopped(session):
        return False, "emergency stop active"
    if account.status not in ("ACTIVE", "PAUSED", "PAUSED_AUTO"):
        return False, f"account status {account.status}"
    hidden = (session.query(Comment).filter(Comment.owned_channel_id == account.id, Comment.status == "HIDDEN",
                                            Comment.fetched_at >= now - _dt.timedelta(hours=24)).count())
    if hidden >= HIDES_PER_ACCOUNT_PER_DAY:
        return False, "daily hide cap"
    return True, ""
