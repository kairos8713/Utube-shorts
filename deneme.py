# run_uploads.py
import datetime, os, sys
sys.path.insert(0, '.')
os.chdir(r'C:\Users\emre8\Desktop\utube')

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import DB_PATH, OwnedChannels, UploadQueue, videos, Sectors
from publisher import YouTubePublisher

engine = create_engine(DB_PATH)
Session = sessionmaker(bind=engine)
sess = Session()

# Tüm PENDING itemları al, kanal ve tarih sırasına göre
pending = (sess.query(UploadQueue)
    .filter_by(status='PENDING')
    .order_by(UploadQueue.owned_channel_id, UploadQueue.scheduled_for)
    .all())

print(f"Toplam {len(pending)} video yüklenecek")

success_count = 0
fail_count = 0

pub = YouTubePublisher(headless=False)

for item in pending:
    sess.expire_all()
    
    ch = sess.query(OwnedChannels).get(item.owned_channel_id)
    vid = sess.query(videos).get(item.video_id)
    sector = sess.query(Sectors).get(ch.sector_id)

    sector_dir = os.path.join('shorts', f'sector_{ch.sector_id}')
    file_path = os.path.join(sector_dir, f'video_{vid.id}.mp4')

    if not os.path.exists(file_path):
        print(f'[SKIP] Dosya yok: {file_path}')
        item.status = 'FAILED'
        item.error_message = 'File not found'
        sess.commit()
        fail_count += 1
        continue

    desc_parts = []
    if sector and sector.description:
        desc_parts.append(sector.description)
    if vid.summary_text:
        desc_parts.append(vid.summary_text)
    if vid.cta_question:
        desc_parts.append(vid.cta_question)
    description = '\n\n'.join(desc_parts)

    print(f"\n[{success_count+fail_count+1}/{len(pending)}] Ch{ch.id} ({ch.name}) → {vid.title}")
    print(f"  Tarih: {item.scheduled_for}")

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
            sess.commit()
            success_count += 1
            print(f"  ✓ SUCCESS")
        else:
            item.status = 'FAILED'
            item.error_message = 'Publisher returned False'
            sess.commit()
            fail_count += 1
            print(f"  ✗ FAILED")

    except Exception as e:
        item.status = 'FAILED'
        item.error_message = str(e)[:500]
        sess.commit()
        fail_count += 1
        print(f"  ✗ ERROR: {e}")

    import time
    time.sleep(3)

print(f"\n=== TAMAMLANDI: {success_count} başarılı, {fail_count} başarısız ===")