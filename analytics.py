"""
analytics.py — relative performance, early warning, metric collection.

relative performance of a post = metric at checkpoint ÷ the account's median of
the same metric over its posts of the last 14 days. Used by clip scoring
feedback, source trials, the slot scheduler and the reports.
"""
import datetime as _dt
import json
import statistics
from collections import defaultdict
from typing import Optional

from log_setup import get_logger
from models import AccountMetric, OwnedChannels, Post, PostMetric, UploadQueue
from timeutil import utcnow

log = get_logger("analytics")

CHECKPOINTS_H = (1, 24, 72)
MEDIAN_WINDOW = _dt.timedelta(days=14)
EARLY_WARNING_RATIO = 0.30
PRIMARY_METRIC = "views"


def metric_at(session, post_id: int, checkpoint_h: int, metric: str = PRIMARY_METRIC) -> Optional[float]:
    row = (session.query(PostMetric).filter_by(post_id=post_id, checkpoint_h=checkpoint_h)
           .order_by(PostMetric.fetched_at.desc()).first())
    if not row:
        return None
    v = getattr(row, metric, None)
    if v is None and metric == "views":
        v = row.reach
    return float(v) if v is not None else None


def account_median(session, account_id: int, checkpoint_h: int = 24, metric: str = PRIMARY_METRIC,
                   now=None, window=MEDIAN_WINDOW) -> Optional[float]:
    now = now or utcnow()
    posts = (session.query(Post).filter(Post.owned_channel_id == account_id, Post.status == "PUBLISHED",
                                        Post.published_at >= now - window).all())
    vals = [v for v in (metric_at(session, p.id, checkpoint_h, metric) for p in posts) if v is not None]
    return statistics.median(vals) if vals else None


def relative(session, post: Post, checkpoint_h: int = 24, metric: str = PRIMARY_METRIC, now=None,
             _median_cache: dict = None) -> Optional[float]:
    v = metric_at(session, post.id, checkpoint_h, metric)
    if v is None:
        return None
    key = (post.owned_channel_id, checkpoint_h, metric)
    if _median_cache is not None and key in _median_cache:
        med = _median_cache[key]
    else:
        med = account_median(session, post.owned_channel_id, checkpoint_h, metric, now)
        if _median_cache is not None:
            _median_cache[key] = med
    if not med:
        return None
    return v / med


def early_warning_active(session, account, now=None) -> bool:
    """3-day relative reach < 30 % of the 14-day median."""
    now = now or utcnow()
    recent = account_median(session, account.id, 24, "reach", now, _dt.timedelta(days=3))
    base = account_median(session, account.id, 24, "reach", now, MEDIAN_WINDOW)
    if recent is None or not base:
        return False
    n_recent = (session.query(Post).filter(Post.owned_channel_id == account.id, Post.status == "PUBLISHED",
                                           Post.published_at >= now - _dt.timedelta(days=3)).count())
    return n_recent >= 2 and recent < EARLY_WARNING_RATIO * base


def slot_performance(session, account_ids, now=None) -> dict:
    """{slot_hour: {"n": posts, "relative": mean relative views@24h, "explore_n", "qwen_n"}}"""
    now = now or utcnow()
    cache = {}
    agg = defaultdict(lambda: {"vals": [], "qwen": [], "explore": []})
    posts = (session.query(Post).filter(Post.owned_channel_id.in_(list(account_ids)), Post.status == "PUBLISHED",
                                        Post.published_at >= now - _dt.timedelta(days=60)).all())
    for p in posts:
        if p.slot_hour is None:
            continue
        r = relative(session, p, 24, now=now, _median_cache=cache)
        if r is None:
            continue
        agg[p.slot_hour]["vals"].append(r)
        if p.slot_source in ("qwen", "explore"):
            agg[p.slot_hour][p.slot_source].append(r)
    out = {}
    for h, d in agg.items():
        out[h] = {"n": len(d["vals"]), "relative": round(statistics.mean(d["vals"]), 3),
                  "qwen_n": len(d["qwen"]), "explore_n": len(d["explore"])}
    return out


def slot_source_comparison(session, since, until=None) -> dict:
    """Qwen-picked vs explore slots: mean relative views@24h (report section)."""
    until = until or utcnow()
    cache, groups = {}, defaultdict(list)
    posts = (session.query(Post).filter(Post.status == "PUBLISHED", Post.published_at >= since,
                                        Post.published_at < until).all())
    for p in posts:
        r = relative(session, p, 24, now=until, _median_cache=cache)
        if r is not None:
            groups[p.slot_source or "default"].append(r)
    return {k: {"n": len(v), "mean_relative": round(statistics.mean(v), 3)} for k, v in groups.items()}


# ── collection (publisher service, every 30 min) ─────────────────────────────

def _store_post_metrics(session, post, checkpoint_h, data, now):
    session.add(PostMetric(
        post_id=post.id, checkpoint_h=checkpoint_h, fetched_at=now,
        views=_int(data.get("views")), reach=_int(data.get("reach")), likes=_int(data.get("likes")),
        comments=_int(data.get("comments")), shares=_int(data.get("shares")), saved=_int(data.get("saved")),
        total_interactions=_int(data.get("total_interactions")),
        avg_watch_time_ms=_int(data.get("ig_reels_avg_watch_time")), raw_json=json.dumps(data)[:4000]))


def _int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def collect_due(session, now=None, client_factory=None) -> dict:
    from instagram.client import client_for
    from instagram.errors import IGError
    now = now or utcnow()
    client_factory = client_factory or client_for
    out = {"post_metrics": 0, "accounts": 0, "errors": 0}
    posts = (session.query(Post).filter(Post.status == "PUBLISHED", Post.dry_run == False,  # noqa: E712
                                        Post.published_at >= now - _dt.timedelta(days=4)).all())
    clients = {}
    for p in posts:
        have = {m.checkpoint_h for m in session.query(PostMetric).filter_by(post_id=p.id).all()}
        for h in CHECKPOINTS_H:
            if h in have or now < p.published_at + _dt.timedelta(hours=h):
                continue
            acc = session.get(OwnedChannels, p.owned_channel_id)
            c = clients.setdefault(acc.id, client_factory(acc))
            try:
                _store_post_metrics(session, p, h, c.media_insights(p.ig_media_id), now)
                out["post_metrics"] += 1
            except IGError as e:
                out["errors"] += 1
                log.warning("insights failed for post %s: %s", p.id, e)
            break       # one checkpoint per post per run (the oldest missing one)
    session.commit()
    from jobs import due, mark
    if due("account_metrics", _dt.timedelta(hours=24), now, session):
        out["accounts"] = collect_accounts(session, now, client_factory)
        mark("account_metrics", now, session)
        review_accounts(session, now)
    return out


def collect_accounts(session, now=None, client_factory=None) -> int:
    from instagram.client import client_for
    from instagram.errors import IGError
    now = now or utcnow()
    n = 0
    for acc in session.query(OwnedChannels).filter(OwnedChannels.status.in_(("ACTIVE", "PAUSED", "PAUSED_AUTO"))).all():
        if not acc.ig_access_token:
            continue
        c = (client_factory or client_for)(acc)
        if c.dry_run:
            continue
        row = AccountMetric(owned_channel_id=acc.id, day=now.strftime("%Y-%m-%d"), fetched_at=now)
        try:
            row.followers = _int(c.me("followers_count").get("followers_count"))
            reach = c.account_insights("reach", period="day", metric_type="total_value")
            row.reach = _int(((reach or [{}])[0].get("total_value") or {}).get("value"))
        except IGError as e:
            log.warning("account metrics failed for %s: %s", acc.id, e)
        for metric, params, attr in (
                ("follower_demographics", {"period": "lifetime", "timeframe": "last_30_days",
                                           "metric_type": "total_value", "breakdown": "country"}, "audience_geo_json"),
                ("online_followers", {"period": "lifetime"}, "online_followers_json")):
            try:          # optional: unavailable under 100 followers / not in the current metric list
                data = c.account_insights(metric, **params)
                setattr(row, attr, json.dumps(data)[:8000])
            except IGError:
                pass
        session.add(row)
        n += 1
    session.commit()
    return n


# ── early warning → Qwen review (decision task, narrow scope) ────────────────

_REVIEW_ROLE = """An Instagram Reels account's recent reach dropped sharply versus its own 14-day
median. Decide ONE action for THIS account: "noop" (keep going — a normal dip), "account_pace"
(daily_post_cap 1 to slow down) or "account_pause" (stop posting; a human will review). Pause only
for strong evidence (e.g. reach collapsed across several posts, possible restriction)."""
_REVIEW_SPEC = """{"actions": [{"type": "noop"} | {"type": "account_pace", "account_id": <id>, "daily_post_cap": 1}
 | {"type": "account_pause", "account_id": <id>, "reason": "<why>"}], "reason": "<short>"}"""


def review_accounts(session, now=None, llm=None) -> dict:
    from alerts import raise_alert
    from brain.core import run_decision
    from prompt_safety import data_block
    from prompt_safety import system_prompt
    now = now or utcnow()
    out = {}
    for acc in session.query(OwnedChannels).filter_by(status="ACTIVE").all():
        if not early_warning_active(session, acc, now):
            continue
        recent = account_median(session, acc.id, 24, "reach", now, _dt.timedelta(days=3))
        base = account_median(session, acc.id, 24, "reach", now)
        raise_alert("EARLY_WARNING", f"Account {acc.ig_username or acc.id}: 3-day reach median {recent:.0f} "
                    f"< 30% of 14-day median {base:.0f}", severity="WARNING", account_id=acc.id, email=False,
                    session=session)
        data = {"account_id": acc.id, "reach_median_3d": recent, "reach_median_14d": base,
                "daily_post_cap": acc.daily_post_cap, "active_days": round((now - (acc.activated_at or now)).days)}
        res = run_decision(session, "account_review", system_prompt(_REVIEW_ROLE, _REVIEW_SPEC),
                           data_block("ACCOUNT_STATS", json.dumps(data)),
                           allowed={"noop", "account_pace", "account_pause"}, ctx={"account_ids": [acc.id]},
                           account_id=acc.id, input_summary=json.dumps(data), llm=llm)
        out[acc.id] = [a["type"] for a in res.applied] or ["rejected/none"]
        if any(a["type"] == "account_pause" for a in res.applied):
            session.query(UploadQueue).filter_by(owned_channel_id=acc.id, status="PENDING").update({"status": "HELD"})
            session.commit()
            raise_alert("ACCOUNT_AUTO_PAUSED", f"Qwen paused {acc.ig_username or acc.id} after early warning: "
                        f"{res.reason}", severity="CRITICAL", account_id=acc.id, session=session)
    return out


def online_followers(session, account_id: int) -> dict:
    row = (session.query(AccountMetric).filter_by(owned_channel_id=account_id)
           .filter(AccountMetric.online_followers_json != None)  # noqa: E711
           .order_by(AccountMetric.fetched_at.desc()).first())
    try:
        return json.loads(row.online_followers_json) if row else {}
    except ValueError:
        return {}
