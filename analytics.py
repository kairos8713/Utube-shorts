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
from models import AccountMetric, OwnedChannels, Post, PostMetric
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


def online_followers(session, account_id: int) -> dict:
    row = (session.query(AccountMetric).filter_by(owned_channel_id=account_id)
           .filter(AccountMetric.online_followers_json != None)  # noqa: E711
           .order_by(AccountMetric.fetched_at.desc()).first())
    try:
        return json.loads(row.online_followers_json) if row else {}
    except ValueError:
        return {}
