"""
migrate_render_time.py
======================
Adds render_time_seconds REAL column to the videos table.
Safe to run multiple times (ALTER TABLE is skipped if column exists).
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "automation.db")
print(f"DB: {DB_PATH}")

con = sqlite3.connect(DB_PATH)
cur = con.cursor()

# Check if column already exists
cur.execute("PRAGMA table_info(videos)")
columns = [row[1] for row in cur.fetchall()]
print(f"Existing columns: {columns}")

if "render_time_seconds" in columns:
    print("Column 'render_time_seconds' already exists — nothing to do.")
else:
    cur.execute("ALTER TABLE videos ADD COLUMN render_time_seconds REAL")
    con.commit()
    print("✓ Column 'render_time_seconds REAL' added to videos table.")

con.close()
