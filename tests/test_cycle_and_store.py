import datetime as dt
import os
import sqlite3

import cycle_runner
import dedup
from helpers import NOW, account, sector, source, video
from models import videos
from production import ClipCandidate, SourceVideo
from production_store import ProductionStore, account_inventory, assign_videos
from synthetic_media import make_source_video


def test_cycle_is_three_days():
    assert cycle_runner.CYCLE_DAYS == 3
    anchor = dt.datetime(2026, 10, 19, 0, 0)
    idx, s, e = cycle_runner.cycle_bounds(anchor, dt.datetime(2026, 10, 25, 1, 0))
    assert idx == 2 and s == dt.datetime(2026, 10, 25) and e == dt.datetime(2026, 10, 28)


def test_compute_needs_two_cycle_buffer(db):
    sec = sector(db)
    a = account(db, "a", sector_obj=sec)
    account(db, "nosetup", sector_obj=sec, status="WARMUP", setup_days_ago=None, active_days=None)
    for i in range(2):
        video(db, a, url=f"u{i}")
    # target = 2 cycles × cap 1 × 3 days = 6 → gap 4; the account without setup date needs nothing
    assert cycle_runner.compute_needs(db, NOW) == {sec.id: 4}


def test_assign_spreads_trial_source_and_prefers_deficit(db):
    sec = sector(db)
    a, b, c = (account(db, n, sector_obj=sec) for n in "abc")
    trial = source(db, status="TRIAL", sector_obj=sec)
    for i in range(3):
        video(db, None, url=f"t{i}", sector_obj=sec, source_id=trial.id)
    assert assign_videos(db) == 3
    owners = {v.owned_channel_id for v in db.query(videos).filter_by(source_id=trial.id)}
    assert owners == {a.id, b.id, c.id}
    assert [account_inventory(db, x.id) for x in (a, b, c)] == [1, 1, 1]


def test_store_persists_clip_with_source_credit(db, tmp_path):
    sec = sector(db, "Podcast")
    src_row = source(db, url="https://www.youtube.com/@RealSrc", sector_obj=sec)
    seg_path = make_source_video(str(tmp_path / "seg.mp4"), duration=40)
    out = str(tmp_path / "reel.mp4")
    with open(out, "wb") as f:
        f.write(b"x")
    src = SourceVideo(url="https://www.youtube.com/watch?v=zz", niche="Podcast", source_id=src_row.id,
                      source_handle="RealSrc")
    cand = ClipCandidate(src, {"path": seg_path, "offset": 60.0}, 5.0, 27.0, 8, topic="t",
                         caption={"caption": "c\n🎥 @RealSrc", "comment": "c", "hashtags": ["#a1", "#b2", "#c3"]},
                         out_path=out, render_info={"duration": 22.0})
    cand.extras = {"phash": dedup.clip_phash(seg_path, 5.0, 27.0)}
    store = ProductionStore(db, cycle_id=1)
    v = store.on_clip_ready(cand)
    assert v.segment_start == 65.0 and v.segment_end == 87.0 and v.caption.endswith("@RealSrc")
    assert len(v.phash.split(",")) == 4
    assert store.is_duplicate(src.url, 70, 90)                    # overlapping segment
    assert not store.is_duplicate(src.url, 200, 222)
    # identical frames from a re-upload under another URL → perceptual duplicate
    assert dedup.clip_conflict(db, "other-url", 0, 22, v.phash)


def test_migrations_on_legacy_schema(tmp_path):
    import db_migrate
    path = str(tmp_path / "legacy.db")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE sectors (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, title VARCHAR NOT NULL,
                              description VARCHAR NOT NULL, tags VARCHAR NOT NULL, voices_path VARCHAR);
        CREATE TABLE channels (id INTEGER PRIMARY KEY, url VARCHAR UNIQUE, last_checked_at DATETIME, sector_id INTEGER);
        CREATE TABLE owned_channels (id INTEGER PRIMARY KEY, sector_id INTEGER, name VARCHAR, level INTEGER NOT NULL,
                              mail VARCHAR NOT NULL, password VARCHAR NOT NULL, is_active BOOLEAN);
        CREATE TABLE videos (id INTEGER PRIMARY KEY, qwen_point INTEGER NOT NULL, caption VARCHAR);
        CREATE TABLE upload_queue (id INTEGER PRIMARY KEY, owned_channel_id INTEGER, video_id INTEGER,
                              scheduled_for DATETIME NOT NULL, status VARCHAR);
        CREATE TABLE sources (id INTEGER PRIMARY KEY, channel_url VARCHAR UNIQUE, handle VARCHAR, sector_id INTEGER,
                              status VARCHAR, status_reason VARCHAR, status_changed_at DATETIME, discovered_at DATETIME,
                              legacy_channel_id INTEGER, bandit_alpha FLOAT, bandit_beta FLOAT, clips_published INTEGER);
        INSERT INTO sectors VALUES (1, 'Tech / AI (Riskli)', 't', 'd', 'x', NULL);
        INSERT INTO channels VALUES (1, 'https://www.youtube.com/@MrBeast', NULL, 1);
        INSERT INTO owned_channels VALUES (1, 1, 'acc', 1, 'm', 'plain', 1);
    """)
    c.commit()
    c.close()
    applied = db_migrate.run(path, log=lambda *_: None)
    assert applied == [n for n, _ in db_migrate.MIGRATIONS]
    c = sqlite3.connect(path)
    cols = {r[1] for r in c.execute("PRAGMA table_info(owned_channels)")}
    assert {"ig_access_token", "ig_setup_completed_at", "status", "daily_post_cap"} <= cols
    assert c.execute("SELECT status, daily_post_cap FROM owned_channels").fetchone() == ("WARMUP", 1)
    assert c.execute("SELECT music_slug FROM sectors").fetchone()[0] == "tech_ai_riskli"
    assert c.execute("SELECT handle, status FROM sources").fetchone() == ("MrBeast", "TRIAL")
    assert {"source_title", "source_published_at"} <= {r[1] for r in c.execute("PRAGMA table_info(videos)")}
    c.close()
    assert db_migrate.run(path, log=lambda *_: None) == []
