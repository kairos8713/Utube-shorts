"""
debug_analytics_join.py
=======================
Step 1: Print DB schemas for relevant tables.
Step 2: Show the JOIN result for the first 5 videos — expose duplicate collapse.
Step 3: Show unique view-count analysis.
Run: python debug_analytics_join.py
"""
import os, sys
sys.path.insert(0, '.')
os.chdir(r'C:\Users\emre8\Desktop\utube')

from sqlalchemy import create_engine, inspect, text
from models import DB_PATH, Base, videos, VideoStats, OwnedChannels, Channels

engine  = create_engine(DB_PATH)

# ── Step 1: Schema dump ───────────────────────────────────────────────────────
print("\n" + "="*60)
print("STEP 1 — TABLE SCHEMAS")
print("="*60)
inspector = inspect(engine)
for tname in ['videos', 'video_stats', 'owned_channels', 'channels', 'processed_source_videos']:
    if tname not in inspector.get_table_names():
        print(f"\n=== {tname} === (NOT FOUND)")
        continue
    print(f"\n=== {tname} ===")
    for col in inspector.get_columns(tname):
        pk  = '  PK' if col.get('primary_key') else ''
        print(f"  {col['name']:35s} {str(col['type']):20s}{pk}")
    for fk in inspector.get_foreign_keys(tname):
        print(f"  FK: {fk['constrained_columns']} → {fk['referred_table']}.{fk['referred_columns']}")

# ── Step 2: Show current (buggy) JOIN results ─────────────────────────────────
print("\n" + "="*60)
print("STEP 2 — CURRENT JOIN RESULT (first 10 rows)")
print("="*60)
from sqlalchemy.orm import sessionmaker
Session = sessionmaker(bind=engine)
sess    = Session()

try:
    rows = sess.query(
        videos.id,
        videos.title,
        videos.qwen_point,
        videos.source_channel_id,
        videos.owned_channel_id,
        VideoStats.views,
        VideoStats.likes,
        VideoStats.comments,
        VideoStats.publish_date,
        OwnedChannels.name.label('target_channel_name'),
    ).join(VideoStats, VideoStats.owned_channel_id == videos.owned_channel_id, isouter=True)\
     .join(OwnedChannels, OwnedChannels.id == videos.owned_channel_id, isouter=True)\
     .filter(videos.is_used == True)\
     .order_by(VideoStats.publish_date.desc())\
     .limit(10)\
     .all()

    print(f"Total rows returned (limit 10): {len(rows)}")
    for r in rows:
        print(f"  vid.id={r.id:<5} qwen={r.qwen_point}  src_ch={r.source_channel_id}  "
              f"owned_ch={r.owned_channel_id}  views={r.views}  "
              f"title={str(r.title or '')[:40]}")
except Exception as e:
    print(f"  ERROR: {e}")

# ── Step 3: Unique view count analysis ────────────────────────────────────────
print("\n" + "="*60)
print("STEP 3 — UNIQUE VIEW COUNT ANALYSIS")
print("="*60)
try:
    total_rows   = len(rows)
    unique_views = len({r.views for r in rows if r.views is not None})
    print(f"  Total video rows:      {total_rows}")
    print(f"  Unique view counts:    {unique_views}  ← if << total_rows, JOIN is collapsing")
except Exception as e:
    print(f"  ERROR: {e}")

# ── Step 4: What VideoStats actually links to ─────────────────────────────────
print("\n" + "="*60)
print("STEP 4 — VIDEO STATS TABLE STRUCTURE (first 5 rows)")
print("="*60)
try:
    with engine.connect() as con:
        result = con.execute(text(
            "SELECT id, owned_channel_id, video_title, views, likes, publish_date "
            "FROM video_stats ORDER BY id LIMIT 5"
        ))
        for row in result:
            print(f"  vs.id={row[0]:<5} owned_ch={row[1]}  views={row[3]}  "
                  f"title={str(row[2] or '')[:40]}  date={row[5]}")
except Exception as e:
    print(f"  ERROR: {e}")

# ── Step 5: What videos table links to ───────────────────────────────────────
print("\n" + "="*60)
print("STEP 5 — VIDEOS TABLE (first 5 is_used=True rows)")
print("="*60)
try:
    with engine.connect() as con:
        result = con.execute(text(
            "SELECT id, owned_channel_id, qwen_point, source_channel_id, title, is_used "
            "FROM videos WHERE is_used=1 ORDER BY id LIMIT 5"
        ))
        for row in result:
            print(f"  vid.id={row[0]:<5} owned_ch={row[1]}  qwen={row[2]}  "
                  f"src_ch={row[3]}  title={str(row[4] or '')[:40]}")
except Exception as e:
    print(f"  ERROR: {e}")

# ── Step 6: Check if VideoStats has a video_id FK column ─────────────────────
print("\n" + "="*60)
print("STEP 6 — DOES video_stats HAVE A video_id COLUMN?")
print("="*60)
vs_cols = [c['name'] for c in inspector.get_columns('video_stats')]
print(f"  video_stats columns: {vs_cols}")
has_vid_id = 'video_id' in vs_cols
print(f"  Has 'video_id' column: {has_vid_id}")
# Note: video_id here is the YouTube video ID string, NOT the FK to videos.id

sess.close()
print("\n[DONE]")
