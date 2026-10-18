"""Factories shared by tests."""
import datetime as dt

from crypto_utils import encrypt
from models import OwnedChannels, Post, Sectors, Source, videos

NOW = dt.datetime(2026, 10, 20, 14, 0)     # UTC, inside the posting window (16:00 Warsaw)


def sector(db, name="Podcast"):
    s = db.query(Sectors).filter_by(name=name).first()
    if s:
        return s
    s = Sectors(name=name, title=name, description=name, tags=name)
    db.add(s)
    db.commit()
    return s


def account(db, name="acc", sector_obj=None, status="ACTIVE", setup_days_ago=20, active_days=20,
            cap=1, token=True):
    sec = sector_obj or sector(db)
    a = OwnedChannels(name=name, sector_id=sec.id, level=1, mail=f"{name}@x", password=encrypt("pw"),
                      status=status, daily_post_cap=cap,
                      ig_setup_completed_at=NOW - dt.timedelta(days=setup_days_ago) if setup_days_ago is not None else None,
                      activated_at=NOW - dt.timedelta(days=active_days) if active_days is not None else None,
                      ig_user_id=f"17841{abs(hash(name)) % 10**9}" if token else None,
                      ig_username=name, ig_access_token=encrypt("tok-" + name) if token else None,
                      ig_token_expires_at=NOW + dt.timedelta(days=50) if token else None,
                      consecutive_failures=0)
    db.add(a)
    db.commit()
    return a


def video(db, acc=None, url="https://www.youtube.com/watch?v=abc", start=10.0, end=32.0, phash=None,
          sector_obj=None, source_id=None, score=8, time_sensitive=False, path=None):
    sec = sector_obj or (db.get(Sectors, acc.sector_id) if acc else sector(db))
    v = videos(sector_id=sec.id, owned_channel_id=acc.id if acc else None, qwen_point=score,
               source_url=url, segment_start=start, segment_end=end, phash=phash, qc_status="PASS",
               caption="A caption.\n\n#a1 #b2 #c3\n🎥 @Src", file_path=path, source_id=source_id,
               time_sensitive=time_sensitive, clip_duration=end - start, is_used=False, created_at=NOW)
    db.add(v)
    db.commit()
    return v


def post(db, acc, vid, status="PUBLISHED", published_at=None, created_at=None):
    p = Post(owned_channel_id=acc.id, video_id=vid.id, status=status,
             published_at=published_at or NOW - dt.timedelta(hours=2),
             created_at=created_at or published_at or NOW - dt.timedelta(hours=2),
             source_url=vid.source_url, segment_start=vid.segment_start, segment_end=vid.segment_end,
             phash=vid.phash, ig_media_id=f"m{vid.id}")
    db.add(p)
    db.commit()
    return p


def source(db, url="https://www.youtube.com/@Src", status="ACTIVE", sector_obj=None, **kw):
    sec = sector_obj or sector(db)
    s = Source(channel_url=url, handle=url.rsplit("@", 1)[-1], sector_id=sec.id, status=status, **kw)
    db.add(s)
    db.commit()
    return s
