"""Migration: Add caption and hashtags columns to videos table."""
import sqlite3

DB = "automation.db"
con = sqlite3.connect(DB)
cur = con.cursor()

try:
    cur.execute("ALTER TABLE videos ADD COLUMN caption TEXT")
    print("Added caption column")
except sqlite3.OperationalError as e:
    print(f"caption skip: {e}")

try:
    cur.execute("ALTER TABLE videos ADD COLUMN hashtags TEXT")
    print("Added hashtags column")
except sqlite3.OperationalError as e:
    print(f"hashtags skip: {e}")

con.commit()
con.close()
print("Migration complete")
