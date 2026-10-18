import sqlite3
import os

db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "automation.db")
con = sqlite3.connect(db_path)
cur = con.cursor()

# Check if column already exists
cur.execute("PRAGMA table_info(videos)")
cols = [row[1] for row in cur.fetchall()]
print(f"[MIGRATE] Current videos columns: {cols}")

if "owned_channel_id" in cols:
    print("[MIGRATE] owned_channel_id already exists — skipping ALTER TABLE.")
else:
    cur.execute(
        "ALTER TABLE videos ADD COLUMN owned_channel_id INTEGER REFERENCES owned_channels(id)"
    )
    con.commit()
    print("[MIGRATE] ✓ Migration done: owned_channel_id added to videos.")

# Verify
cur.execute("PRAGMA table_info(videos)")
final_cols = [(row[0], row[1], row[2]) for row in cur.fetchall()]
print("[MIGRATE] Final schema:")
for c in final_cols:
    print(f"  {c[0]:>3}  {c[1]:<25} {c[2]}")

con.close()
