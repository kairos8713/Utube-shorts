"""
fix_owned_channel_id.py
=======================
Backfills owned_channel_id for existing videos that have NULL.
Matches by sector_id, assigns up to 7 videos per channel, ordered by video id.
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "automation.db")
print(f"DB: {DB_PATH}")

con = sqlite3.connect(DB_PATH)
cur = con.cursor()

# Check how many NULL videos exist before
cur.execute("SELECT COUNT(*) FROM videos WHERE owned_channel_id IS NULL")
before = cur.fetchone()[0]
print(f"\nVideos with NULL owned_channel_id BEFORE: {before}")

# For each owned channel, assign their sector's videos
# that don't have an owner yet, in order of video id
cur.execute("""
    SELECT oc.id, oc.sector_id, oc.name 
    FROM owned_channels oc 
    ORDER BY oc.id
""")
channels = cur.fetchall()

if not channels:
    print("No owned channels found — nothing to do.")
    con.close()
    raise SystemExit(0)

print(f"\nFound {len(channels)} owned channel(s):\n")

total_assigned = 0
for ch_id, sector_id, name in channels:
    # Find unowned videos in this sector
    cur.execute("""
        SELECT id FROM videos 
        WHERE sector_id = ? AND owned_channel_id IS NULL
        ORDER BY id
        LIMIT 7
    """, (sector_id,))
    video_ids = [r[0] for r in cur.fetchall()]

    for vid_id in video_ids:
        cur.execute("""
            UPDATE videos SET owned_channel_id = ? WHERE id = ?
        """, (ch_id, vid_id))

    total_assigned += len(video_ids)
    print(f"  Channel '{name}' (id={ch_id}, sector={sector_id}): assigned {len(video_ids)} video(s)")

con.commit()

# Verify after
cur.execute("SELECT COUNT(*) FROM videos WHERE owned_channel_id IS NULL")
after = cur.fetchone()[0]

con.close()

print(f"\n{'='*50}")
print(f"Total assigned : {total_assigned} video(s)")
print(f"NULL remaining : {after}")
print(f"{'='*50}")
