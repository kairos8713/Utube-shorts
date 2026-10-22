import datetime as dt
import json
import random

import guardrails
import scheduler
import system_state
from fakes import FakeHost, FakeIG
from helpers import NOW, account, post, sector, video
from models import PostMetric, UploadQueue
from publish_runner import publish_due
from timeutil import slot_key_of, window_day_of, window_for


def _queue(db, acc_id=None):
    q = db.query(UploadQueue)
    if acc_id:
        q = q.filter_by(owned_channel_id=acc_id)
    return q.order_by(UploadQueue.scheduled_for).all()


def test_schedule_respects_window_cap_and_global_gap(db):
    sec = sector(db)
    accs = [account(db, f"s{i}", sector_obj=sec) for i in range(3)]
    for a in accs:
        for i in range(8):
            video(db, a, url=f"{a.name}-{i}")
    rng = random.Random(7)
    for a in accs:
        assert scheduler.schedule_account(db, a, NOW, rng) == scheduler.HORIZON_DAYS
    times = [q.scheduled_for for q in _queue(db)]
    assert all(window_day_of(t) is not None for t in times)             # always inside 15:00–05:00
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    assert min(gaps) >= guardrails.MIN_GAP_BETWEEN_POSTS.total_seconds()
    for a in accs:                                                      # cap 1 per window-day
        days = [window_day_of(q.scheduled_for) for q in _queue(db, a.id)]
        assert len(days) == len(set(days))


def test_exact_explore_share_with_qwen_plan(db):
    a = account(db, "ex")
    for i in range(8):
        video(db, a, url=f"e{i}")
    system_state.put(f"slot_plan:{a.id}", json.dumps({"slots": [19, 21], "at": NOW.isoformat()}), db)
    scheduler.schedule_account(db, a, NOW, random.Random(1), horizon_days=8)
    rows = _queue(db, a.id)
    assert len(rows) == 8
    src = [r.slot_source for r in rows]
    assert src.count("explore") == 2 and src.count("qwen") == 6
    for r in rows:
        k = slot_key_of(r.scheduled_for)
        assert (k in (19, 21)) == (r.slot_source == "qwen")


def test_warmup_account_starts_after_gate(db):
    a = account(db, "w", status="WARMUP", setup_days_ago=1, active_days=None)
    video(db, a)
    scheduler.schedule_account(db, a, NOW, random.Random(2))
    q = _queue(db, a.id)[0]
    assert q.scheduled_for >= a.ig_setup_completed_at + guardrails.WARMUP


def test_dst_day_schedule(db):
    a = account(db, "dst")
    for i in range(3):
        video(db, a, url=f"d{i}")
    now = dt.datetime(2026, 10, 24, 10, 0)
    scheduler.schedule_account(db, a, now, random.Random(3), horizon_days=2)
    first = _queue(db, a.id)[0].scheduled_for
    s, e = window_for(dt.date(2026, 10, 24))
    assert s <= first < e and e == dt.datetime(2026, 10, 25, 4, 0)


def test_missed_posts_are_spread_not_bulk(db):
    a = account(db, "m", cap=2, active_days=30)
    for i in range(4):
        v = video(db, a, url=f"m{i}")
        db.add(UploadQueue(owned_channel_id=a.id, video_id=v.id, status="PENDING",
                           scheduled_for=NOW - dt.timedelta(hours=10 - i)))
    db.commit()
    now = NOW + dt.timedelta(hours=1)                    # 17:00 Warsaw, window open until 05:00
    out = scheduler.reschedule_missed(db, now, random.Random(4))
    assert out["respread"] == 2 and out["requeued"] == 2   # cap 2 for today, rest → end of queue
    rows = _queue(db, a.id)
    _, w_end = window_for(window_day_of(now))
    today = [r for r in rows if r.scheduled_for < w_end]
    assert len(today) == 2 and all(r.scheduled_for > now for r in rows)
    assert (today[1].scheduled_for - today[0].scheduled_for) > dt.timedelta(hours=1)


def test_plan_slots_needs_four_posts_per_slot(db):
    a = account(db, "plan")
    for i in range(10):                      # 5 posts in slot 19 (rel 2.0), 5 in slot 23 (rel ~1)
        slot = 19 if i % 2 else 23
        v = video(db, a, url=f"p{i}")
        p = post(db, a, v, published_at=NOW - dt.timedelta(days=i + 1))
        p.slot_hour, p.slot_source = slot, "default"
        db.add(PostMetric(post_id=p.id, checkpoint_h=24, views=2000 if slot == 19 else 1000, reach=1))
    db.commit()
    plan = scheduler.plan_slots(db, NOW)
    assert plan[a.id][0] == 19
    assert scheduler.qwen_slots(db, a.id, NOW) == plan[a.id]


def test_fast_lane_insert_priority_takes_first_slot(db):
    a = account(db, "fl")
    for i in range(3):
        video(db, a, url=f"n{i}")
    scheduler.schedule_account(db, a, NOW, random.Random(5))
    urgent = video(db, a, url="news", time_sensitive=True, score=7)
    scheduler.insert_priority(db, a, NOW, random.Random(6))
    rows = _queue(db, a.id)
    assert rows[0].video_id == urgent.id and rows[0].priority == 1 and len(rows) == 4


def test_publish_due_keeps_8_minute_gap(db, tmp_path):
    a, b = account(db, "a"), account(db, "b")
    for acc in (a, b):
        f = tmp_path / f"{acc.name}.mp4"
        f.write_bytes(b"0" * 100)
        v = video(db, acc, url=acc.name, path=str(f))
        db.add(UploadQueue(owned_channel_id=acc.id, video_id=v.id, scheduled_for=NOW, status="PENDING"))
    db.commit()
    out = publish_due(db, NOW, random.Random(1), client_factory=lambda acc, **k: FakeIG(), host=FakeHost(),
                      sleep=lambda s: None)
    assert [o for _, o in out] == ["PUBLISHED"]
    out2 = publish_due(db, NOW + dt.timedelta(minutes=2), random.Random(1),
                       client_factory=lambda acc, **k: FakeIG(), host=FakeHost(), sleep=lambda s: None)
    assert [o for _, o in out2] == ["DEFER_GAP"]
    waiting = db.query(UploadQueue).filter_by(status="PENDING").one()
    assert waiting.scheduled_for >= NOW + guardrails.MIN_GAP_BETWEEN_POSTS


def test_eight_day_simulation_one_post_per_window_day_14h_gap_no_day_lost(db, tmp_path):
    """Decision #2: cap per Warsaw posting-window day + MIN_GAP_SAME_ACCOUNT, random minute in the slot.
    8 window days (20–27 Oct, incl. the DST night of 25 Oct), 4 accounts, 5 seeds: every account gets
    exactly one post on every window day, ≥ 14 h apart, and every planned post publishes on time."""
    from collections import Counter
    sec = sector(db)
    from models import Post
    for seed in range(5):
        db.query(Post).delete()                  # each seed replays the same 8 days from scratch
        db.query(UploadQueue).delete()
        db.commit()
        accs = [account(db, f"sim{seed}_{i}", sector_obj=sec) for i in range(4)]
        for a in accs:
            for i in range(10):
                f = tmp_path / f"{a.name}_{i}.mp4"
                f.write_bytes(b"0" * 100)
                video(db, a, url=f"{a.name}-{i}", path=str(f))
        rng = random.Random(seed)
        for a in accs:
            assert scheduler.schedule_account(db, a, NOW, rng, horizon_days=8) == 8
        items = [q for a in accs for q in _queue(db, a.id)]
        outcomes = []
        for t in sorted({q.scheduled_for for q in items}):
            outcomes += [o for _, o in publish_due(db, t, random.Random(1), client_factory=lambda acc, **k: FakeIG(),
                                                   host=FakeHost(), sleep=lambda s: None)]
        assert outcomes == ["PUBLISHED"] * 32, Counter(outcomes)          # no DEFER_CAP / DEFER_GAP
        for a in accs:
            times = sorted(q.scheduled_for for q in _queue(db, a.id))
            days = [window_day_of(t) for t in times]
            assert days == [dt.date(2026, 10, 20) + dt.timedelta(days=d) for d in range(8)]   # 0 days lost
            assert min(b - x for x, b in zip(times, times[1:])) >= dt.timedelta(hours=14)
            assert all(q.status == "UPLOADED" for q in _queue(db, a.id))


def test_guardrails_page_shows_the_new_cap_rules():
    rows = {k: (v, d) for k, v, d in guardrails.describe()}
    assert "penceresi günü" in rows["DAILY_CAP_START"][1]
    assert rows["MIN_GAP_SAME_ACCOUNT"][0] == "cap 1: 14 saat · cap 2: 6 saat"
    assert guardrails.min_gap_same_account(1) == dt.timedelta(hours=14)
    assert guardrails.min_gap_same_account(2) == dt.timedelta(hours=6)
