"""
maintenance.py — disk hygiene and backups.

* cleanup_after_publish(): delete the published reel + cover; delete the source
  download and its segments once no unpublished clip of that source video is
  left.
* daily(): temp/work cleanup, stale downloads/segments/samples, disk threshold
  alarm (< DISK_ALERT_PCT free), daily DB backup to backups/daily (14 kept).
  The DB already stores tokens/passwords Fernet-encrypted; the FERNET_KEY is
  deliberately NOT copied into backups (backup + key together = plaintext).
"""
import datetime as _dt
import glob
import os
import shutil
import sqlite3

from alerts import raise_alert
from config import ROOT_DIR, settings
from db_migrate import sqlite_path
from downloader import video_id_from_url
from log_setup import get_logger
from models import videos
from timeutil import utcnow

log = get_logger("maintenance")

BACKUP_KEEP_DAYS = 14
STALE_DOWNLOAD_DAYS = 7


def _rm(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
            return True
    except OSError as e:
        log.warning("could not delete %s: %s", path, e)
    return False


def source_files(url: str) -> list:
    vid = video_id_from_url(url)
    out = glob.glob(os.path.join(settings.DOWNLOAD_DIR, f"{vid}.*"))
    out += glob.glob(os.path.join(settings.SEGMENTS_DIR, f"{vid}_part*.mp4"))
    return out


def cleanup_after_publish(session, video) -> int:
    n = 0
    for p in (video.file_path, video.cover_path):
        n += _rm(p)
    video.files_deleted_at = utcnow()
    if video.source_url:
        remaining = (session.query(videos).filter(videos.source_url == video.source_url, videos.is_used == False,  # noqa: E712
                                                  videos.qc_status == "PASS", videos.id != video.id).count())
        if remaining == 0:
            for f in source_files(video.source_url):
                n += _rm(f)
    session.commit()
    return n


def cleanup_stale(now=None) -> int:
    now = now or utcnow()
    n = 0
    # temp render dirs (renderer cleans its own; leftovers after crashes)
    for d in glob.glob(os.path.join(settings.WORK_DIR, "reel_*")) + glob.glob(os.path.join(settings.WORK_DIR, "ffr_*")):
        if now.timestamp() - os.path.getmtime(d) > 86400:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    for f in glob.glob(os.path.join(settings.WORK_DIR, "samples", "*")):
        if now.timestamp() - os.path.getmtime(f) > 86400:
            n += _rm(f)
    for folder in (settings.DOWNLOAD_DIR, settings.SEGMENTS_DIR):
        for f in glob.glob(os.path.join(folder, "*")):
            if now.timestamp() - os.path.getmtime(f) > STALE_DOWNLOAD_DAYS * 86400:
                n += _rm(f)
    return n


def disk_free_pct(path: str = ROOT_DIR) -> float:
    du = shutil.disk_usage(path)
    return du.free / du.total * 100


def check_disk(session) -> float:
    pct = disk_free_pct()
    if pct < settings.DISK_ALERT_PCT:
        raise_alert("DISK_LOW", f"Only {pct:.1f}% disk free on {ROOT_DIR}", severity="CRITICAL", session=session)
    return pct


def backup_db(now=None, db_path: str = None) -> str:
    now = now or utcnow()
    db_path = db_path or sqlite_path()
    dst_dir = os.path.join(settings.BACKUP_DIR, "daily")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, f"automation_{now:%Y%m%d}.db")
    src = sqlite3.connect(db_path)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    if os.path.getsize(dst) == 0:
        raise RuntimeError("empty backup")
    cutoff = now - _dt.timedelta(days=BACKUP_KEEP_DAYS)
    for f in glob.glob(os.path.join(dst_dir, "automation_*.db")):
        try:
            day = _dt.datetime.strptime(os.path.basename(f)[11:19], "%Y%m%d")
        except ValueError:
            continue
        if day < cutoff:
            _rm(f)
    return dst


def daily(session, now=None) -> dict:
    now = now or utcnow()
    out = {"cleaned": cleanup_stale(now), "disk_free_pct": round(check_disk(session), 1)}
    try:
        out["backup"] = backup_db(now)
    except Exception as e:
        raise_alert("BACKUP_FAILED", f"Daily DB backup failed: {e}", severity="CRITICAL", session=session)
        out["backup_error"] = str(e)
    log.info("daily maintenance: %s", out)
    return out
