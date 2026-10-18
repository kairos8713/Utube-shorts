"""
migrate_video_id_fk.py
======================
Idempotent migration that:
  1. Adds `videos.yt_video_id`     (VARCHAR 20, nullable)
  2. Adds `video_stats.videos_fk_id` (INTEGER FK → videos.id, nullable)
  3. Creates indexes on both new columns.

Run once:
    python migrate_video_id_fk.py

Safe to run multiple times — checks column existence before ALTER TABLE.
"""

import sqlite3
import os
import re
import unicodedata
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE  = os.path.join(BASE_DIR, "automation.db")

# ── helpers ───────────────────────────────────────────────────────────────────

def column_exists(cur, table, col):
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == col for row in cur.fetchall())

def index_exists(cur, name):
    cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (name,))
    return cur.fetchone() is not None

def normalize(t):
    """Lowercase, strip, remove emoji/special chars, truncate to 80."""
    if not t:
        return ""
    t = t.strip().lower()
    # Remove emoji and non-ASCII
    t = ''.join(c for c in t if unicodedata.category(c)[0] != 'So' and ord(c) < 0xFFFF)
    # Collapse whitespace
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:80]

# ── STEP 1: Schema migration ──────────────────────────────────────────────────

def run_schema_migration():
    print(f"\n[MIGRATE] Connecting to: {DB_FILE}")
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()

    # 1a. videos.yt_video_id
    if not column_exists(cur, 'videos', 'yt_video_id'):
        cur.execute("ALTER TABLE videos ADD COLUMN yt_video_id VARCHAR(20)")
        print("[+] videos.yt_video_id added")
    else:
        print("[=] videos.yt_video_id already exists — skipped")

    # 1b. video_stats.videos_fk_id
    if not column_exists(cur, 'video_stats', 'videos_fk_id'):
        cur.execute("ALTER TABLE video_stats ADD COLUMN videos_fk_id INTEGER REFERENCES videos(id)")
        print("[+] video_stats.videos_fk_id added")
    else:
        print("[=] video_stats.videos_fk_id already exists — skipped")

    # 1c. Indexes
    if not index_exists(cur, 'idx_videos_yt_video_id'):
        cur.execute("CREATE INDEX idx_videos_yt_video_id ON videos(yt_video_id)")
        print("[+] idx_videos_yt_video_id created")
    else:
        print("[=] idx_videos_yt_video_id already exists — skipped")

    if not index_exists(cur, 'idx_video_stats_videos_fk_id'):
        cur.execute("CREATE INDEX idx_video_stats_videos_fk_id ON video_stats(videos_fk_id)")
        print("[+] idx_video_stats_videos_fk_id created")
    else:
        print("[=] idx_video_stats_videos_fk_id already exists — skipped")

    con.commit()
    con.close()
    print("[MIGRATE] Schema migration complete.")

# ── STEP 2: Backfill videos_fk_id by title+channel matching ──────────────────

def run_backfill():
    print("\n[MIGRATION] Starting backfill of video_stats.videos_fk_id ...")
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Load all VideoStats rows where videos_fk_id IS NULL
    cur.execute("""
        SELECT id, owned_channel_id, video_title, publish_date
        FROM   video_stats
        WHERE  videos_fk_id IS NULL
    """)
    stat_rows = cur.fetchall()
    total = len(stat_rows)
    print(f"[MIGRATION]   VideoStats rows to process: {total}")

    if total == 0:
        print("[MIGRATION]   Nothing to backfill.")
        con.close()
        return

    # Load all videos (is_used=True) from our table
    cur.execute("""
        SELECT id, owned_channel_id, title, yt_video_id
        FROM   videos
        WHERE  is_used = 1
    """)
    video_rows = cur.fetchall()

    # Build lookup: owned_channel_id → list of (video_id, norm_title)
    from collections import defaultdict
    ch_videos = defaultdict(list)
    for v in video_rows:
        ch_videos[v['owned_channel_id']].append({
            'id':    v['id'],
            'norm':  normalize(v['title']),
        })

    matched   = 0
    ambiguous = 0
    unmatched = 0

    for stat in stat_rows:
        stat_id    = stat['id']
        ch_id      = stat['owned_channel_id']
        norm_title = normalize(stat['video_title'])

        candidates = [
            v for v in ch_videos.get(ch_id, [])
            if v['norm'] == norm_title
        ]

        if len(candidates) == 1:
            cur.execute(
                "UPDATE video_stats SET videos_fk_id=? WHERE id=?",
                (candidates[0]['id'], stat_id)
            )
            matched += 1
        elif len(candidates) > 1:
            # Pick the one with smallest id as a tiebreak (oldest production)
            best = min(candidates, key=lambda x: x['id'])
            print(f"[MIGRATION]   AMBIGUOUS stat.id={stat_id} title='{stat['video_title'][:50]}' "
                  f"→ {len(candidates)} matches, picking videos.id={best['id']}")
            cur.execute(
                "UPDATE video_stats SET videos_fk_id=? WHERE id=?",
                (best['id'], stat_id)
            )
            ambiguous += 1
            matched += 1
        else:
            print(f"[MIGRATION]   NO MATCH: stat.id={stat_id} ch={ch_id} "
                  f"title='{stat['video_title'][:50]}'")
            unmatched += 1

    con.commit()
    con.close()

    print(f"\n[MIGRATION] Backfill complete")
    print(f"[MIGRATION]   Total VideoStats rows:     {total}")
    print(f"[MIGRATION]   Matched (video_id set):    {matched}")
    print(f"[MIGRATION]   Ambiguous (multi-match):   {ambiguous}")
    print(f"[MIGRATION]   Unmatched (kept NULL):     {unmatched}")


# ── STEP 3: Backfill videos.yt_video_id from video_stats.video_id ────────────
# video_stats.video_id already stores the YouTube string ID.
# We can copy it to videos.yt_video_id where the FK join is now established.

def run_yt_id_backfill():
    print("\n[MIGRATION] Backfilling videos.yt_video_id from video_stats.video_id ...")
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()

    cur.execute("""
        UPDATE videos
        SET    yt_video_id = (
            SELECT vs.video_id
            FROM   video_stats vs
            WHERE  vs.videos_fk_id = videos.id
              AND  vs.video_id IS NOT NULL
              AND  vs.video_id != ''
            LIMIT 1
        )
        WHERE  yt_video_id IS NULL
    """)
    updated = cur.rowcount
    con.commit()
    con.close()
    print(f"[MIGRATION]   videos.yt_video_id populated for {updated} rows.")


if __name__ == '__main__':
    run_schema_migration()
    run_backfill()
    run_yt_id_backfill()
    print("\n[MIGRATE] All done.")
