import os, sys, time, datetime, subprocess, glob, sqlite3, json
try:
    import pytz
    POLAND_TZ = pytz.timezone('Europe/Warsaw')
except ImportError:
    pytz = None
    POLAND_TZ = None

def poland_15h_to_utc(date):
    """Polonya saatiyle 15:00'i UTC'ye çevirir (yaz/kış saati otomatik)."""
    if POLAND_TZ is not None:
        # Polonya saatiyle 15:00 naive datetime oluştur, sonra UTC'ye çevir
        local_naive = date.replace(hour=15, minute=0, second=0, microsecond=0)
        local_aware = POLAND_TZ.localize(local_naive, is_dst=None)
        utc_aware = local_aware.astimezone(pytz.utc)
        return utc_aware.replace(tzinfo=None)  # naive UTC döndür
    else:
        # pytz yoksa statik offset: Polonya yaz saati UTC+2 varsay
        return date.replace(hour=13, minute=0, second=0, microsecond=0)
sys.path.insert(0, '.')
os.chdir(r'C:\Users\Emre\Desktop\utube - Kopya')

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from models import DB_PATH, OwnedChannels, UploadQueue, videos, Sectors
from publisher import YouTubePublisher

engine = create_engine(DB_PATH)
Session = sessionmaker(bind=engine)

PYTHON = sys.executable
CHANNELS = list(range(1, 21))  # 20 channels

STATUS_FILE = 'weekly_cycle_status.json'

def _update_ytdlp_once():
    """Run yt-dlp self-update ONCE per cycle, not per download."""
    print("[CYCLE] Updating yt-dlp to latest version (once per cycle) ...")
    subprocess.run(
        [PYTHON, "-m", "pip", "install", "-U", "yt-dlp"],
        check=False, capture_output=True,
    )
    print("[CYCLE] yt-dlp update complete.")


def update_status(phase, **kwargs):
    status = {
        'phase': phase,
        'updated_at': datetime.datetime.utcnow().isoformat(),
        **kwargs
    }
    with open(STATUS_FILE, 'w') as f:
        json.dump(status, f, default=str)
    print(f"[CYCLE] Status: {phase} {kwargs}")

# Sistem kapalı kalma süresi: eğer son tarih bu kadar günden eskiyse yoksay
_MAX_STALE_DAYS = 14

def get_last_upload_date():
    """Get the last scheduled_for date from UPLOADED videos only.

    Priority:
    1. Max scheduled_for from UPLOADED records (taze veya eski farketmez, en güvenilir kaynak)
    2. last_cycle_date.txt (file fallback — önceki döngüde kaydedilen)
    3. utcnow() (last resort)

    NOT: PENDING kayıtlara BAKILMAZ — döngü her başladığında PENDING tarihler
    zaten mevcut olup bunları base_date olarak almak tarihi 1 hafta ileri kaydırır.
    """
    now = datetime.datetime.utcnow()
    sess = Session()

    # 1) UPLOADED records — en güvenilir kaynak, stale check yok
    uploaded_result = sess.query(UploadQueue.scheduled_for)\
        .filter_by(status='UPLOADED')\
        .order_by(UploadQueue.scheduled_for.desc())\
        .first()
    sess.close()

    if uploaded_result:
        dt = uploaded_result[0]
        if isinstance(dt, str):
            dt = datetime.datetime.fromisoformat(dt)
        with open('last_cycle_date.txt', 'w') as f:
            f.write(dt.isoformat())
        print(f"[CYCLE] Base date from UPLOADED records: {dt.date()}")
        return dt

    # 2) Fallback to file (önceki döngüde kaydedilen UPLOADED tarih)
    try:
        with open('last_cycle_date.txt') as f:
            dt = datetime.datetime.fromisoformat(f.read().strip())
        print(f"[CYCLE] Base date from last_cycle_date.txt: {dt.date()}")
        return dt
    except:
        pass

    # 3) Son çare: utcnow+1 (YouTube en az 15dk ilerisine izin verir, güvenli buffer için +1 gün)
    dt = now + datetime.timedelta(days=1)
    print(f"[CYCLE] Base date fallback to utcnow+1: {dt.date()}")
    return dt

def cleanup_db():
    sess = Session()
    pending_count = sess.query(UploadQueue).filter_by(status='PENDING').count()
    sess.close()
    print(f"[CYCLE] DB check: {pending_count} pending entries in queue")

def produce_all_channels():
    """Run fulfillment for all 20 channels, retry until all are full."""
    from models import videos as Videos, OwnedChannels
    
    while True:
        print(f"\n[CYCLE] Checking channel inventory...")
        sess = Session()
        channels = sess.query(OwnedChannels).order_by(OwnedChannels.id).all()
        
        gaps = []
        for ch in channels:
            count = sess.query(Videos).filter_by(
                owned_channel_id=ch.id, is_used=False
            ).count()
            if count < 7:
                gaps.append((ch.id, ch.name, 7 - count))
        sess.close()
        
        if not gaps:
            print(f"[CYCLE] All 20 channels full. Production complete.")
            break
        
        print(f"[CYCLE] {len(gaps)} channel(s) need videos: {[(g[0], g[2]) for g in gaps]}")
        
        all_skipped = True
        for ch_id, ch_name, gap in gaps:
            print(f"\n[CYCLE] Producing channel #{ch_id} ({ch_name}) gap={gap}...")
            update_status('PRODUCING', channel=ch_id, total=20, 
                         remaining=len(gaps))
            cmd = [PYTHON, 'sector_processor.py',
                   '--fulfillment', f'--channel-id={ch_id}', 
                   f'--gap={gap}']
            result = subprocess.run(cmd, timeout=28800)
            
            # Check if something was actually produced
            sess = Session()
            new_count = sess.query(Videos).filter_by(
                owned_channel_id=ch_id, is_used=False
            ).count()
            sess.close()
            
            # Use > instead of >= to ensure we actually produced something. 
            # Otherwise, new_count == (7 - gap) would trigger False and cause an infinite loop.
            if new_count > (7 - gap):
                all_skipped = False
        
        if all_skipped:
            print(f"[CYCLE] All channels skipped (pool exhausted?). Moving on.")
            break
    
    print(f"\n[CYCLE] Production phase complete.")

def fix_upload_dates(base_date):
    """Deduplicate PENDING entries per channel, then set upload dates."""
    sess = Session()

    # -- Step 1: Remove duplicates -- keep only 7 newest per channel ----------
    ch_ids = [row[0] for row in
              sess.query(UploadQueue.owned_channel_id).distinct().all()]
    for ch_id in ch_ids:
        dupes = (
            sess.query(UploadQueue)
            .filter_by(owned_channel_id=ch_id, status='PENDING')
            .order_by(UploadQueue.created_at.desc())
            .all()
        )
        if len(dupes) > 7:
            for d in dupes[7:]:
                sess.delete(d)
    sess.commit()

    # -- Step 2: Assign upload dates ------------------------------------------
    # start en az YARIN (UTC+1 gün) olsun.
    # Bugün atanırsa: first_scheduled = bugün 15:00 → upload_start_time = dün 15:00 (geçmiş)
    # → wait_until anında geçer → upload başlatılır veya bir sonraki döngüye döner.
    tomorrow = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
    start = max(base_date + datetime.timedelta(days=1), tomorrow)
    pending = (
        sess.query(UploadQueue)
        .filter_by(status='PENDING')
        .order_by(UploadQueue.owned_channel_id, UploadQueue.created_at)
        .all()
    )

    channel_day = {}
    for item in pending:
        ch_id = item.owned_channel_id
        if ch_id not in channel_day:
            channel_day[ch_id] = 0
        new_date = start + datetime.timedelta(days=channel_day[ch_id])
        item.scheduled_for = poland_15h_to_utc(new_date)
        channel_day[ch_id] += 1

    sess.commit()
    last = max((item.scheduled_for for item in pending), default=start)
    sess.close()
    print(f"[CYCLE] Scheduled {len(pending)} videos from {start.date()} to {last.date()}")
    return last

def upload_all_pending():
    """Upload all pending videos using deneme.py logic."""
    sess = Session()
    pending = sess.query(UploadQueue)\
        .filter_by(status='PENDING')\
        .order_by(UploadQueue.owned_channel_id, UploadQueue.scheduled_for)\
        .all()
    
    print(f"\n[CYCLE] Uploading {len(pending)} videos...")
    update_status('UPLOADING', total=len(pending))
    
    pub = YouTubePublisher(headless=False)
    success = 0
    fail = 0
    
    for item in pending:
        sess.expire_all()
        ch = sess.query(OwnedChannels).get(item.owned_channel_id)
        vid = sess.query(videos).get(item.video_id)
        sector = sess.query(Sectors).get(ch.sector_id)
        
        sector_dir = os.path.join('reels', f'sector_{ch.sector_id}')
        file_path = os.path.join(sector_dir, f'video_{vid.id}.mp4')
        
        if not os.path.exists(file_path):
            print(f"[SKIP] File missing: {file_path}")
            item.status = 'FAILED'
            item.error_message = 'File not found'
            sess.commit()
            fail += 1
            continue
        
        desc_parts = []
        if sector and sector.description:
            desc_parts.append(sector.description)
        if vid.summary_text:
            desc_parts.append(vid.summary_text)
        if vid.cta_question:
            desc_parts.append(vid.cta_question)
        description = '\n\n'.join(desc_parts)
        
        print(f"\n[{success+fail+1}/{len(pending)}] Ch{ch.id} ({ch.name})")
        print(f"  Title: {vid.title}")
        print(f"  Scheduled: {item.scheduled_for}")
        
        try:
            ok = pub.upload_video(
                video_path=os.path.abspath(file_path),
                title=vid.title or 'Video',
                description=description,
                tags=sector.tags or '',
                js_path=os.path.abspath(ch.js_path),
                user_agent=ch.user_agent or 'Mozilla/5.0',
                schedule_datetime=item.scheduled_for,
            )
            if ok:
                item.status = 'UPLOADED'
                item.uploaded_at = datetime.datetime.utcnow()
                vid.is_used = True
                sess.commit()
                success += 1
                print(f"  ✓ SUCCESS")
            else:
                item.status = 'FAILED'
                item.error_message = 'Publisher returned False'
                sess.commit()
                fail += 1
                print(f"  ✗ FAILED")
        except Exception as e:
            item.status = 'FAILED'
            item.error_message = str(e)[:500]
            sess.commit()
            fail += 1
            print(f"  ✗ ERROR: {e}")
        
        time.sleep(3)
    
    sess.close()
    print(f"\n[CYCLE] Upload complete: {success} success, {fail} failed")
    return success, fail

def cleanup_reels():
    """Delete all .mp4 files from reels folder."""
    update_status('CLEANUP')
    files = glob.glob('reels/**/*.mp4', recursive=True)
    for f in files:
        try:
            os.remove(f)
        except:
            pass
    print(f"[CYCLE] Cleaned up {len(files)} video files")

def wait_until(target_dt):
    """Wait until target datetime."""
    now = datetime.datetime.utcnow()
    if target_dt > now:
        wait_secs = (target_dt - now).total_seconds()
        print(f"[CYCLE] Waiting {wait_secs/3600:.1f} hours until {target_dt}")
        time.sleep(wait_secs)

def run_cycle():
    """Main weekly cycle loop."""
    print("[CYCLE] Weekly cycle started")

    # Update yt-dlp ONCE here — not per download
    _update_ytdlp_once()

    # Get last upload date BEFORE any cleanup, saved to file as backup
    last_upload = get_last_upload_date()
    print(f"[CYCLE] Last upload date: {last_upload.date() if last_upload else 'None'}")

    while True:
        update_status('IDLE')

        # Cleanup leftover PENDING entries from previous failed cycles
        # UPLOADED records are preserved for date tracking
        cleanup_db()

        # Produce videos for next week
        produce_all_channels()

        # Deduplicate + assign upload dates starting from last_upload + 1 day
        last_scheduled = fix_upload_dates(last_upload)

        # Wait until 1 day before the FIRST scheduled upload at 15:00 UTC
        sess = Session()
        first_scheduled = sess.query(func.min(UploadQueue.scheduled_for))\
            .filter_by(status='PENDING').scalar()
        sess.close()

        if first_scheduled is None:
            print("[CYCLE] No PENDING entries found — skipping wait.")
            first_scheduled = last_scheduled

        if isinstance(first_scheduled, str):
            first_scheduled = datetime.datetime.fromisoformat(first_scheduled)

        upload_start_time = first_scheduled - datetime.timedelta(days=1)
        upload_start_time = poland_15h_to_utc(upload_start_time)
        update_status('WAITING', next_upload=str(upload_start_time), last_scheduled=str(last_scheduled))
        wait_until(upload_start_time)

        # Upload all pending
        upload_all_pending()

        # Cleanup reels folder
        cleanup_reels()

        # Re-read last_upload from DB (now reflects newly UPLOADED records)
        last_upload = get_last_upload_date()
        print(f"[CYCLE] Next cycle base date: {last_upload.date() if last_upload else 'None'}")

        print(f"\n[CYCLE] Cycle complete. Starting next cycle...\n")


if __name__ == '__main__':
    run_cycle()
