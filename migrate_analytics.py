"""
migrate_analytics.py
====================
Adds token_path column to owned_channels and pre-populates it.

Rule: 2 channels share one Gmail token file.
  ch 1-2  → tokens/gmail_1_token.json
  ch 3-4  → tokens/gmail_2_token.json
  ...

Run once:
    python migrate_analytics.py
"""

import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE  = os.path.join(BASE_DIR, "automation.db")


def column_exists(cursor, table, column):
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def run():
    print(f"[MIGRATE] Connecting to: {DB_FILE}")
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()

    # 1. Add column if missing
    if not column_exists(cur, "owned_channels", "token_path"):
        cur.execute("ALTER TABLE owned_channels ADD COLUMN token_path TEXT")
        print("[+] owned_channels.token_path added")
    else:
        print("[=] owned_channels.token_path already exists — skipped")

    # 2. owned_channels.youtube_channel_id
    if not column_exists(cur, "owned_channels", "youtube_channel_id"):
        cur.execute("ALTER TABLE owned_channels ADD COLUMN youtube_channel_id TEXT")
        print("[+] owned_channels.youtube_channel_id added")
    else:
        print("[=] owned_channels.youtube_channel_id already exists — skipped")

    # 3. owned_channels.subs_count
    if not column_exists(cur, "owned_channels", "subs_count"):
        cur.execute("ALTER TABLE owned_channels ADD COLUMN subs_count INTEGER DEFAULT 0")
        print("[+] owned_channels.subs_count added")
    else:
        print("[=] owned_channels.subs_count already exists — skipped")

    # 4. video_stats new columns
    vs_columns = [
        ("video_id",                  "TEXT",         None),
        ("traffic_source_shorts",     "REAL",         0),
        ("traffic_source_search",     "REAL",         0),
        ("traffic_source_suggested",  "REAL",         0),
    ]
    for col_name, col_type, default in vs_columns:
        if not column_exists(cur, "video_stats", col_name):
            ddl = f"ALTER TABLE video_stats ADD COLUMN {col_name} {col_type}"
            if default is not None:
                ddl += f" DEFAULT {default}"
            cur.execute(ddl)
            print(f"[+] video_stats.{col_name} added")
        else:
            print(f"[=] video_stats.{col_name} already exists — skipped")

    # 5. processed_source_videos — failure tracking columns
    psv_columns = [
        ("failure_count",  "INTEGER", 0),
        ("last_failed_at", "TIMESTAMP", None),
        ("failure_reason", "TEXT",    None),
    ]
    for col_name, col_type, default in psv_columns:
        if not column_exists(cur, "processed_source_videos", col_name):
            ddl = f"ALTER TABLE processed_source_videos ADD COLUMN {col_name} {col_type}"
            if default is not None:
                ddl += f" DEFAULT {default}"
            cur.execute(ddl)
            print(f"[+] processed_source_videos.{col_name} added")
        else:
            print(f"[=] processed_source_videos.{col_name} already exists — skipped")

    # 4. Pre-populate token_path for all channels
    #    Channels 1-2 → gmail_1, 3-4 → gmail_2, etc.
    cur.execute("SELECT id FROM owned_channels ORDER BY id")
    rows = cur.fetchall()

    updated = 0
    for (ch_id,) in rows:
        gmail_idx  = (ch_id - 1) // 2 + 1
        token_path = f"tokens/gmail_{gmail_idx}_token.json"
        cur.execute(
            "UPDATE owned_channels SET token_path=? WHERE id=?",
            (token_path, ch_id)
        )
        print(f"  ch {ch_id:>2} → {token_path}")
        updated += 1

    con.commit()
    con.close()
    print(f"\n[MIGRATE] Done. {updated} channels token_path updated.")


if __name__ == "__main__":
    run()
