import datetime as dt
import glob
import os

import email_notifier
import maintenance
import power
import system_state
import watchdog
from helpers import NOW, account, post, video
from models import Alert, UploadQueue


# ── e-mail ───────────────────────────────────────────────────────────────────

def test_email_dedup_6h_and_outbox():
    t0 = dt.datetime(2026, 11, 1, 12, 0)
    assert email_notifier.send_alert("DISK_LOW", "s", "b", key="dd", now=t0) == "outbox"
    assert email_notifier.send_alert("DISK_LOW", "s", "b", key="dd", now=t0 + dt.timedelta(hours=5)) == "deduped"
    assert email_notifier.send_alert("DISK_LOW", "s", "b", key="dd", now=t0 + dt.timedelta(hours=6, minutes=1)) == "outbox"
    assert email_notifier.send_alert("SOMETHING_MINOR", "s", "b") == "not_emergency"
    assert glob.glob(os.path.join(os.environ["LOG_DIR"], "outbox", "*DISK_LOW*.eml"))


def test_email_disabled(monkeypatch):
    monkeypatch.setenv("EMAIL_ENABLED", "false")
    assert email_notifier.send_alert("DISK_LOW", "s", "b", key="x2") == "disabled"


# ── maintenance ──────────────────────────────────────────────────────────────

def test_cleanup_after_publish_keeps_shared_source_until_last_clip(db, tmp_path, monkeypatch):
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "dl"))
    monkeypatch.setenv("SEGMENTS_DIR", str(tmp_path / "seg"))
    os.makedirs(tmp_path / "dl")
    os.makedirs(tmp_path / "seg")
    src_file = tmp_path / "dl" / "abc123.mp4"
    seg_file = tmp_path / "seg" / "abc123_part1.mp4"
    for f in (src_file, seg_file):
        f.write_bytes(b"x")
    a = account(db, "m")
    url = "https://www.youtube.com/watch?v=abc123"
    reels = []
    for i in range(2):
        r = tmp_path / f"reel{i}.mp4"
        r.write_bytes(b"x")
        reels.append(video(db, a, url=url, start=i * 30, end=i * 30 + 22, path=str(r)))
    reels[0].is_used = True
    maintenance.cleanup_after_publish(db, reels[0])
    assert not os.path.exists(reels[0].file_path) and src_file.exists()     # clip #2 still needs the source
    reels[1].is_used = True
    maintenance.cleanup_after_publish(db, reels[1])
    assert not src_file.exists() and not seg_file.exists()


def test_backup_rotation(tmp_root):
    for d in range(16):
        path = maintenance.backup_db(dt.datetime(2026, 10, 1) + dt.timedelta(days=d))
    files = glob.glob(os.path.join(os.environ["BACKUP_DIR"], "daily", "automation_*.db"))
    assert os.path.getsize(path) > 0 and len(files) <= maintenance.BACKUP_KEEP_DAYS + 1


def test_disk_low_alert(db, monkeypatch):
    monkeypatch.setattr(maintenance, "disk_free_pct", lambda path=None: 4.0)
    maintenance.check_disk(db)
    assert db.query(Alert).filter_by(kind="DISK_LOW").count() == 1


def test_power_flags_do_not_crash():
    assert power.prevent_sleep() in (True, False)
    assert power.allow_sleep() in (True, False)


# ── watchdog ─────────────────────────────────────────────────────────────────

def test_watchdog_restarts_stale_service_with_limit(db):
    restarted = []
    system_state.put("heartbeat:publisher", (NOW - dt.timedelta(minutes=30)).isoformat(), db)
    system_state.put("heartbeat:comment_responder", NOW.isoformat(), db)
    fake = lambda script, pid=None: restarted.append(script) or True
    for _ in range(4):
        out = watchdog.check_services(db, NOW, fake)
    assert restarted == ["publish_runner.py"] * 3 and out["publisher"] == "limit"
    assert out["comment_responder"] == "ok" and out["cycle_runner"] == "never_started"
    assert db.query(Alert).filter_by(kind="SERVICE_RESTART_FAILED").count() == 1


def test_watchdog_panel_and_ollama(db, monkeypatch):
    restarted = []
    fake = lambda script, pid=None: restarted.append(script) or True
    assert watchdog.check_panel(db, NOW, http_get=lambda url: 200, restarter=fake) == "ok"
    assert watchdog.check_panel(db, NOW, http_get=lambda url: (_ for _ in ()).throw(OSError()), restarter=fake) == "restarted"
    monkeypatch.setenv("LLM_BACKEND", "ollama")
    state = {"up": False}
    assert watchdog.check_ollama(db, NOW, health=lambda: state["up"],
                                 starter=lambda: state.update(up=True)) == "restarted"
    assert watchdog.check_ollama(db, NOW, health=lambda: False, starter=lambda: None) == "failed"


def test_watchdog_no_uploads_in_window(db):
    a = account(db, "wd")
    for i in range(3):
        db.add(UploadQueue(owned_channel_id=a.id, video_id=video(db, a, url=f"w{i}").id, status="PENDING",
                           scheduled_for=NOW + dt.timedelta(hours=3) - dt.timedelta(hours=i + 1)))
    db.commit()
    now = NOW + dt.timedelta(hours=3)                 # 19:00 Warsaw, inside the window
    assert watchdog.check_publishing(db, now) == "alert"
    post(db, a, video(db, a, url="ok"), published_at=now - dt.timedelta(hours=1))
    assert watchdog.check_publishing(db, now) == "ok"
