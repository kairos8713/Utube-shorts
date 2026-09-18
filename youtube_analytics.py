"""
youtube_analytics.py
====================
Fetches per-video analytics for all owned channels via YouTube Data API v3.
Uses an API key — no OAuth required.

Scheduler: runs every 30 minutes in a daemon thread.
    from youtube_analytics import start_scheduler
    start_scheduler(interval_minutes=30)

Manual run:
    python youtube_analytics.py
"""

import os
import sys
import time
import datetime
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from googleapiclient.discovery import build

from models import DB_PATH, OwnedChannels, VideoStats, videos as VideoModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ── API Key ───────────────────────────────────────────────────────────────────
YOUTUBE_API_KEY = 'AIzaSyA48rm3kh7IV4taAjboKgvUmLThz4I05Fc'

engine = create_engine(DB_PATH)
Session = sessionmaker(bind=engine)


# ── Per-channel fetch ─────────────────────────────────────────────────────────

def fetch_channel_analytics(youtube_channel_id: str) -> dict:
    """
    Fetch analytics for a single channel using YouTube Data API v3 (API key).
    Returns a stats dict or None on error.
    """
    try:
        youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)

        # Subscriber count
        subs = 0
        try:
            ch_resp = youtube.channels().list(
                part='statistics',
                id=youtube_channel_id
            ).execute()
            if ch_resp.get('items'):
                subs = int(ch_resp['items'][0]['statistics'].get('subscriberCount', 0))
        except Exception as e:
            print(f"[WARN] Subscriber fetch failed: {e}")

        # Get uploads playlist ID
        uploads_playlist_id = None
        try:
            cd_resp = youtube.channels().list(
                part='contentDetails',
                id=youtube_channel_id
            ).execute()
            if cd_resp.get('items'):
                uploads_playlist_id = (
                    cd_resp['items'][0]['contentDetails']
                    ['relatedPlaylists']['uploads']
                )
        except Exception as e:
            print(f"[WARN] Could not get uploads playlist: {e}")

        if not uploads_playlist_id:
            return {'subs': subs, 'views_lifetime': 0, 'video_records': []}

        # Fetch up to 50 most recent video IDs from uploads playlist
        video_ids = []
        try:
            pl_resp = youtube.playlistItems().list(
                part='contentDetails',
                playlistId=uploads_playlist_id,
                maxResults=50
            ).execute()
            video_ids = [
                item['contentDetails']['videoId']
                for item in pl_resp.get('items', [])
            ]
        except Exception as e:
            print(f"[WARN] Playlist fetch failed: {e}")

        if not video_ids:
            return {'subs': subs, 'views_lifetime': 0, 'video_records': []}

        # Fetch video stats + snippets in one call
        video_records = []
        try:
            stats_resp = youtube.videos().list(
                part='snippet,statistics',
                id=','.join(video_ids)
            ).execute()

            for item in stats_resp.get('items', []):
                vid_id  = item['id']
                snippet = item.get('snippet', {})
                stats   = item.get('statistics', {})

                title = snippet.get('title', '')
                pub   = snippet.get('publishedAt', '')
                date_str = None
                if pub:
                    d = datetime.datetime.fromisoformat(pub.replace('Z', '+00:00'))
                    date_str = d.strftime('%d/%m/%Y')

                views    = int(stats.get('viewCount',    0) or 0)
                likes    = int(stats.get('likeCount',    0) or 0)
                comments = int(stats.get('commentCount', 0) or 0)

                video_records.append({
                    'video_id':    vid_id,
                    'title':       title,
                    'date_str':    date_str,
                    'views':       views,
                    'watch_time':  0.0,   # not available via Data API v3
                    'subs':        0,     # not available via Data API v3
                    'subs_lost':   0,
                    'likes':       likes,
                    'comments':    comments,
                    'shares':      0,
                    'impressions': 0,
                    'ctr':         0.0,
                    'avg_duration':   '',
                    'avg_percentage': 0.0,
                    'traffic_source_shorts':    0.0,
                    'traffic_source_search':    0.0,
                    'traffic_source_suggested': 0.0,
                })

        except Exception as e:
            print(f"[WARN] Video stats fetch failed: {e}")

        # Sort by views descending (mirrors Analytics API behaviour)
        video_records.sort(key=lambda r: r['views'], reverse=True)

        total_views = sum(r['views'] for r in video_records)

        # Calculate 90-day views from publish dates
        cutoff_90d = datetime.datetime.utcnow() - datetime.timedelta(days=90)
        views_90d = 0
        for r in video_records:
            if r.get('date_str'):
                try:
                    pub_dt = datetime.datetime.strptime(r['date_str'], '%d/%m/%Y')
                    if pub_dt >= cutoff_90d:
                        views_90d += r['views']
                except Exception:
                    pass

        return {
            'subs':           subs,
            'views_lifetime': total_views,
            'views_90d':      views_90d,
            'video_records':  video_records,
        }

    except Exception as e:
        print(f"[ERROR] fetch_channel_analytics: {e}")
        return None


# ── Full update loop ──────────────────────────────────────────────────────────

def fetch_all_channels_analytics():
    """Fetch analytics for all channels and save to DB."""
    print(f"\n--- Analytics Update {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")

    sess     = Session()
    channels = sess.query(OwnedChannels).order_by(OwnedChannels.id).all()

    success = 0
    failed  = 0

    for ch in channels:
        print(f"\n[UPDATE] Ch{ch.id} ({ch.name})...")

        if not ch.youtube_channel_id:
            print(f"  [SKIP] No youtube_channel_id — run migrate_db.py to populate")
            failed += 1
            continue

        result = fetch_channel_analytics(ch.youtube_channel_id)

        if not result or not result.get('video_records'):
            print(f"  [SKIP] No data returned — keeping existing records")
            failed += 1
            continue

        try:
            sess.query(VideoStats).filter_by(owned_channel_id=ch.id).delete()

            saved = 0
            for rec in result['video_records']:
                title = (rec.get('title') or '').strip()
                if not title or title.lower().startswith('untitled'):
                    continue

                yt_vid_id = rec.get('video_id')   # YouTube string ID e.g. 'dQw4w9WgXcQ'

                # Find matching videos row by yt_video_id (exact, fast path)
                matched_vid = None
                if yt_vid_id:
                    matched_vid = (
                        sess.query(VideoModel)
                            .filter(VideoModel.yt_video_id == yt_vid_id,
                                    VideoModel.owned_channel_id == ch.id)
                            .first()
                    )
                    # Fallback: if our videos row has no yt_video_id yet, match by title
                    if not matched_vid:
                        import re as _re, unicodedata as _u
                        def _norm(t):
                            if not t: return ''
                            t = t.strip().lower()
                            t = ''.join(c for c in t if _u.category(c)[0]!='So' and ord(c)<0xFFFF)
                            return _re.sub(r'\s+', ' ', t).strip()[:80]
                        matched_vid = (
                            sess.query(VideoModel)
                                .filter(VideoModel.owned_channel_id == ch.id,
                                        VideoModel.is_used == True)
                                .all()
                        )
                        matched_vid = next(
                            (v for v in matched_vid if _norm(v.title) == _norm(title)),
                            None
                        )
                        # If found via title, stamp yt_video_id on the videos row
                        if matched_vid and not matched_vid.yt_video_id:
                            matched_vid.yt_video_id = yt_vid_id
                            print(f"  [UPLOAD] Saved YouTube video ID: {yt_vid_id} "
                                  f"for video #{matched_vid.id} (backfill)")

                vs = VideoStats(
                    owned_channel_id = ch.id,
                    videos_fk_id     = matched_vid.id if matched_vid else None,
                    video_id         = yt_vid_id,
                    video_title      = title,
                    publish_date     = rec.get('date_str'),
                    views            = rec.get('views', 0),
                    watch_time_hours = rec.get('watch_time', 0) / 60,
                    subs_gained      = rec.get('subs', 0),
                    subs_lost        = rec.get('subs_lost', 0),
                    likes            = rec.get('likes', 0),
                    comments         = rec.get('comments', 0),
                    shares           = rec.get('shares', 0),
                    impressions      = rec.get('impressions', 0),
                    ctr              = rec.get('ctr', 0),
                    average_view_duration    = rec.get('avg_duration', ''),
                    average_view_percentage  = rec.get('avg_percentage', 0),
                    traffic_source_shorts    = rec.get('traffic_source_shorts', 0),
                    traffic_source_search    = rec.get('traffic_source_search', 0),
                    traffic_source_suggested = rec.get('traffic_source_suggested', 0),
                )
                if not matched_vid:
                    print(f"  [STATS] No video found for yt_video_id={yt_vid_id}, "
                          f"stat row inserted with videos_fk_id=NULL")
                sess.add(vs)
                saved += 1

            # Update channel total views
            if result['views_lifetime'] > 0:
                ch.sum_views = result['views_lifetime']

            sess.commit()
            print(f"  -> ✓ {saved} videos saved, {result['views_lifetime']} total views")

            # Update channel totals
            ch.subs_count = result.get('subs', 0)
            ch.sum_views  = result.get('views_lifetime', 0)
            ch.m3_views   = result.get('views_90d', 0)
            sess.commit()

            # YPP qualification check (1000 subs + 10M views/90d for Shorts)
            subs_now   = result.get('subs', 0)
            views_90d  = result.get('views_90d', 0)
            if subs_now >= 1000 and views_90d >= 10_000_000 and not ch.is_ypp:
                print(f"[YPP] Channel {ch.id} ({ch.name}) qualifies!")
                try:
                    from send_alert import send_alert
                    send_alert(
                        f"YPP Başvurusu - {ch.name}",
                        f"Kanal YPP kriterlerini karşıladı!\n"
                        f"Kanal: {ch.name}\n"
                        f"Abone: {subs_now:,}\n"
                        f"Son 90 gün izlenme: {views_90d:,}"
                    )
                except Exception as e:
                    print(f"[YPP] Email error: {e}")

            success += 1

        except Exception as e:
            print(f"  [ERROR] DB save failed: {e}")
            sess.rollback()
            failed += 1

        time.sleep(0.5)  # Respect API rate limits

    sess.close()
    print(f"\n--- Done: {success} success, {failed} failed ---")


# ── Scheduler ─────────────────────────────────────────────────────────────────

_scheduler_started = False


def start_scheduler(interval_minutes: int = 30):
    """Start a daemon thread that runs fetch_all_channels_analytics() every N minutes."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def _worker():
        time.sleep(60)  # First run after 1 minute startup delay
        while True:
            try:
                fetch_all_channels_analytics()
            except Exception as e:
                print(f"[SCHEDULER] Error: {e}")
            time.sleep(interval_minutes * 60)

    t = threading.Thread(target=_worker, daemon=True, name="AnalyticsScheduler")
    t.start()
    print(f"[INFO] Analytics scheduler started (interval: {interval_minutes}min)")


if __name__ == '__main__':
    fetch_all_channels_analytics()
