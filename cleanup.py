"""
cleanup.py  —  Run once to purge all test data.
Usage: python cleanup.py
"""
import sqlite3, os, glob

BASE = os.path.dirname(os.path.abspath(__file__))

# ── DATABASE ──────────────────────────────────────────────────────────────────
print("=" * 60)
print("DATABASE CLEANUP")
print("=" * 60)
con = sqlite3.connect(os.path.join(BASE, "automation.db"))
cur = con.cursor()
for tbl in ["videos", "processed_source_videos", "logs", "upload_queue", "video_stats"]:
    cur.execute(f"DELETE FROM {tbl}")
    print(f"  [{tbl}] deleted {cur.rowcount} rows")
con.commit()

# Verify
print("\n  Row counts after cleanup:")
for tbl in ["videos", "processed_source_videos", "logs", "upload_queue",
            "video_stats", "channels", "owned_channels", "sectors"]:
    try:
        cur.execute(f"SELECT COUNT(*) FROM {tbl}")
        print(f"    {tbl}: {cur.fetchone()[0]} rows remaining")
    except Exception as e:
        print(f"    {tbl}: ERROR {e}")
con.close()

# ── FILES ─────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("FILE CLEANUP")
print("=" * 60)
deleted = 0

def rm(path):
    global deleted
    try:
        os.remove(path)
        print(f"  del {os.path.relpath(path, BASE)}")
        deleted += 1
    except Exception as e:
        print(f"  SKIP {os.path.relpath(path, BASE)}: {e}")

# 1. shorts — all .mp4 recursively (including sector subdirs)
shorts_dir = os.path.join(BASE, "shorts")
for f in glob.glob(os.path.join(shorts_dir, "**", "*.mp4"), recursive=True):
    rm(f)

# 2. downloads — everything
downloads_dir = os.path.join(BASE, "downloads")
for fname in os.listdir(downloads_dir):
    rm(os.path.join(downloads_dir, fname))

# 3. segments — everything
segments_dir = os.path.join(BASE, "segments")
for fname in os.listdir(segments_dir):
    rm(os.path.join(segments_dir, fname))

# 4. tts_cache — all .wav
tts_dir = os.path.join(BASE, "tts_cache")
for f in glob.glob(os.path.join(tts_dir, "*.wav")):
    rm(f)

# 5. root dir — orphan TEMP_MPY temp files (.wav, .mp3)
for pattern in ["*TEMP_MPY_wvf_snd*", "temp-audio*"]:
    for f in glob.glob(os.path.join(BASE, pattern)):
        rm(f)

print(f"\n  Total files deleted: {deleted}")

# ── VERIFY ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("VERIFICATION — files remaining")
print("=" * 60)
for folder in ["shorts", "downloads", "segments", "tts_cache"]:
    path = os.path.join(BASE, folder)
    all_files = [f for f in glob.glob(os.path.join(path, "**", "*"), recursive=True)
                 if os.path.isfile(f)]
    print(f"  {folder}/: {len(all_files)} files")

print("\nDone. bg_music/, automation.db, and owned_channels were NOT touched.")
