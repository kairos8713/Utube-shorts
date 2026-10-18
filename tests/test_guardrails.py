import datetime as dt
import inspect

import guardrails as g
import system_state
from helpers import NOW, account, post, video
from models import Comment, CommentReply


def test_constants_cannot_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("DAILY_CAP_ABSOLUTE_MAX", "50")
    import importlib
    importlib.reload(g)
    assert g.DAILY_CAP_ABSOLUTE_MAX == 2
    names = [n for n, _ in inspect.getmembers(g, inspect.isfunction)]
    assert not [n for n in names if n.startswith("set_")]


def test_warmup_gate(db):
    a = account(db, "w1", status="WARMUP", setup_days_ago=None, active_days=None)
    assert not g.warmup_ok(a, NOW)[0] and "not set" in g.warmup_ok(a, NOW)[1]
    a.ig_setup_completed_at = NOW - dt.timedelta(days=1)
    assert not g.warmup_ok(a, NOW)[0]
    a.ig_setup_completed_at = NOW - dt.timedelta(days=2, minutes=1)
    assert g.warmup_ok(a, NOW)[0]


def test_warmup_to_active_only_by_gate_and_never_unpauses(db):
    a = account(db, "w2", status="WARMUP", setup_days_ago=3, active_days=None)
    assert g.refresh_account_status(a, NOW) and a.status == "ACTIVE"
    p = account(db, "p1", status="PAUSED_AUTO")
    assert not g.refresh_account_status(p, NOW) and p.status == "PAUSED_AUTO"


def test_cap_rules(db):
    young = account(db, "young", active_days=5)
    old = account(db, "old", active_days=15)
    assert g.validate_cap_change(db, young, 2, NOW)
    assert g.validate_cap_change(db, old, 2, NOW) == []
    assert g.validate_cap_change(db, old, 3, NOW)
    assert g.validate_cap_change(db, old, 0, NOW)
    old.consecutive_failures = 1
    assert g.validate_cap_change(db, old, 2, NOW)          # unhealthy
    young.daily_post_cap = 2                                # even if the DB says 2…
    assert g.effective_cap(db, young, NOW) == 1             # …guardrail clamps to 1


def test_can_publish_daily_cap_and_global_gap(db):
    a = account(db, "a")
    b = account(db, "b")
    va, vb, vb2 = video(db, a, url="u1"), video(db, b, url="u2"), video(db, b, url="u3")
    assert g.can_publish(db, a, NOW, va)[0]
    post(db, a, va, published_at=NOW - dt.timedelta(minutes=3))
    ok, why = g.can_publish(db, b, NOW, vb)
    assert not ok and "8-minute" in why                      # another account posted 3 min ago
    assert g.can_publish(db, b, NOW + dt.timedelta(minutes=6), vb)[0]
    ok, why = g.can_publish(db, a, NOW + dt.timedelta(hours=1), video(db, a, url="u9"))
    assert not ok and "daily cap" in why                     # cap 1 / 24 h


def test_emergency_stop_blocks_everything(db):
    a = account(db, "e")
    v = video(db, a)
    system_state.set_emergency_stop(True, session=db)
    assert g.can_publish(db, a, NOW, v) == (False, "emergency stop active")
    assert not g.can_hide(db, a, NOW)[0]
    system_state.set_emergency_stop(False, session=db)
    assert g.can_publish(db, a, NOW, v)[0]


def test_same_segment_never_goes_to_two_accounts(db):
    a, b = account(db, "a"), account(db, "b")
    v1 = video(db, a, url="same", start=100, end=122)
    post(db, a, v1, published_at=NOW - dt.timedelta(hours=5))
    v2 = video(db, b, url="same", start=110, end=132)
    ok, why = g.can_publish(db, b, NOW, v2)
    assert not ok and "segment already posted" in why
    # a re-upload under another URL: perceptual hash match on all frames blocks it too
    v_old = video(db, a, url="zz", phash="fe,ff,ff,ff")
    post(db, a, v_old, published_at=NOW - dt.timedelta(hours=30))
    v3 = video(db, b, url="other", start=0, end=22, phash="ff,ff,ff,ff")
    ok, why = g.can_publish(db, b, NOW, v3)
    assert not ok and "perceptual" in why


def test_unconnected_or_expired_token_blocks(db):
    a = account(db, "nt", token=False)
    assert "not connected" in g.can_publish(db, a, NOW)[1]
    b = account(db, "exp")
    b.ig_token_expires_at = NOW - dt.timedelta(minutes=1)
    assert "expired" in g.can_publish(db, b, NOW)[1]


def test_reply_caps_and_48h_rule(db):
    a = account(db, "r")
    v = video(db, a)
    p = post(db, a, v, published_at=NOW - dt.timedelta(days=3))
    c = Comment(ig_comment_id="c1", post_id=p.id, owned_channel_id=a.id, text="hi",
                commented_at=p.published_at + dt.timedelta(hours=49))
    db.add(c)
    db.commit()
    assert "48 h" in g.can_reply(db, a, p, c, NOW)[1]
    c.commented_at = p.published_at + dt.timedelta(hours=2)
    for i in range(g.REPLIES_PER_ACCOUNT_PER_HOUR):
        db.add(CommentReply(comment_id=c.id, owned_channel_id=a.id, text="x", status="SENT",
                            scheduled_for=NOW, sent_at=NOW - dt.timedelta(minutes=10)))
    db.commit()
    assert "hourly" in g.can_reply(db, a, p, c, NOW)[1]
    assert g.can_reply(db, a, p, c, NOW + dt.timedelta(hours=2))[0]
