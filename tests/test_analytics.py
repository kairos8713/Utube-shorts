import datetime as dt

import analytics as an
from helpers import NOW, account, post, video
from llm_client import set_llm
from llm_mock import MockLLM
from models import Alert, OwnedChannels, PostMetric, UploadQueue


class InsightsIG:
    dry_run = False

    def __init__(self):
        self.calls = []

    def media_insights(self, media_id):
        self.calls.append(media_id)
        return {"views": 1234, "reach": 900, "likes": 50, "saved": 3, "ig_reels_avg_watch_time": 8100}

    def me(self, fields=None):
        return {"followers_count": 321}

    def account_insights(self, metric, **params):
        return [{"name": metric, "total_value": {"value": 4000}}]


def test_collect_checkpoints_1_24_72(db):
    a = account(db, "an")
    p = post(db, a, video(db, a), published_at=NOW - dt.timedelta(hours=30))
    ig = InsightsIG()
    an.collect_due(db, NOW, client_factory=lambda acc: ig)
    an.collect_due(db, NOW, client_factory=lambda acc: ig)
    an.collect_due(db, NOW, client_factory=lambda acc: ig)       # 72 h not reached yet
    hs = sorted(m.checkpoint_h for m in db.query(PostMetric).filter_by(post_id=p.id))
    assert hs == [1, 24]
    m = db.query(PostMetric).filter_by(post_id=p.id, checkpoint_h=24).one()
    assert m.views == 1234 and m.avg_watch_time_ms == 8100


def test_dry_run_posts_are_not_queried(db):
    a = account(db, "dry")
    p = post(db, a, video(db, a), published_at=NOW - dt.timedelta(hours=30))
    p.dry_run = True
    db.commit()
    ig = InsightsIG()
    an.collect_due(db, NOW, client_factory=lambda acc: ig)
    assert ig.calls == []


def test_relative_performance_vs_account_median(db):
    a = account(db, "rel")
    ps = []
    for i, v in enumerate([100, 200, 300, 900]):
        p = post(db, a, video(db, a, url=f"r{i}"), published_at=NOW - dt.timedelta(days=i + 1))
        db.add(PostMetric(post_id=p.id, checkpoint_h=24, views=v, reach=v))
        ps.append(p)
    db.commit()
    assert an.account_median(db, a.id, 24, now=NOW) == 250
    assert an.relative(db, ps[3], 24, now=NOW) == 900 / 250


def _reach_history(db, a, recent, base):
    for i in range(8):
        days = 1 + i * 1.5
        v = recent if days <= 3 else base
        p = post(db, a, video(db, a, url=f"h{a.id}-{i}"), published_at=NOW - dt.timedelta(days=days))
        db.add(PostMetric(post_id=p.id, checkpoint_h=24, views=v, reach=v))
    db.commit()


def test_early_warning_triggers_qwen_review_and_pause(db):
    llm = MockLLM()
    set_llm(llm)
    try:
        a = account(db, "ew")
        _reach_history(db, a, recent=50, base=1000)
        assert an.early_warning_active(db, a, NOW)
        llm.responses["account_review"] = {"actions": [{"type": "account_pause", "account_id": a.id,
                                                        "reason": "reach collapsed on every recent post"}],
                                           "reason": "collapse"}
        out = an.review_accounts(db, NOW, llm)
        assert out[a.id] == ["account_pause"]
        assert db.get(OwnedChannels, a.id).status == "PAUSED_AUTO"
        kinds = {x.kind for x in db.query(Alert).all()}
        assert {"EARLY_WARNING", "ACCOUNT_AUTO_PAUSED"} <= kinds
    finally:
        set_llm(None)


def test_no_early_warning_for_normal_account(db):
    a = account(db, "norm")
    _reach_history(db, a, recent=900, base=1000)
    assert not an.early_warning_active(db, a, NOW)


def test_qwen_vs_explore_comparison(db):
    a = account(db, "cmp")
    for i, (src, v) in enumerate([("qwen", 2000), ("qwen", 1800), ("explore", 900), ("explore", 1100)]):
        p = post(db, a, video(db, a, url=f"c{i}"), published_at=NOW - dt.timedelta(days=i + 1))
        p.slot_source = src
        db.add(PostMetric(post_id=p.id, checkpoint_h=24, views=v))
    db.commit()
    cmp_ = an.slot_source_comparison(db, NOW - dt.timedelta(days=10), NOW)
    assert cmp_["qwen"]["mean_relative"] > cmp_["explore"]["mean_relative"]
