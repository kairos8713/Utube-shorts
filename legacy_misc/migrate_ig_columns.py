import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'automation.db')

# Define the expected columns and their SQLite types/defaults
EXPECTED_SCHEMA = {
    "owned_channels": {
        "ig_account_id": "VARCHAR",
        "ig_session_path": "VARCHAR",
        "channel_pool_index": "INTEGER DEFAULT 0",
        "proxy": "VARCHAR",
        "is_active": "BOOLEAN DEFAULT 1",
        "voice_id": "VARCHAR DEFAULT 'af_sky'",
        "persona": "VARCHAR DEFAULT 'Analytical Expert'",
        "font_style": "VARCHAR DEFAULT 'Impact'",
        "accent_color_1": "VARCHAR DEFAULT '#1E90FF'",
        "accent_color_2": "VARCHAR DEFAULT '#FF6D00'",
        "sfx_set": "VARCHAR DEFAULT 'default'"
    },
    "videos": {
        "source_channel_id": "INTEGER",
        "ig_reel_id": "VARCHAR",
        "caption": "VARCHAR",
        "hashtags": "VARCHAR"
    },
    "sectors": {
        "title": "VARCHAR DEFAULT 'Default Title'",
        "description": "VARCHAR DEFAULT 'Default Desc'",
        "tags": "VARCHAR DEFAULT 'default'",
        "voices_path": "VARCHAR"
    },
    "upload_queue": {
        # Ensuring upload_queue table exists or specific columns if it was added recently
        "cycle_id": "INTEGER"
    }
}

def migrate():
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print(f"Connected to {DB_PATH}. Checking for missing columns...\n")

    changes_made = False

    for table, expected_columns in EXPECTED_SCHEMA.items():
        # Check if table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not cursor.fetchone():
            print(f"Table '{table}' does not exist (skip or let SQLAlchemy create it).")
            continue

        # Get existing columns
        cursor.execute(f"PRAGMA table_info({table})")
        existing_cols = {row[1] for row in cursor.fetchall()}

        for col_name, col_def in expected_columns.items():
            if col_name not in existing_cols:
                alter_stmt = f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def};"
                print(f"[ADD] {table}.{col_name} ({col_def})")
                cursor.execute(alter_stmt)
                changes_made = True

    if changes_made:
        conn.commit()
        print("\nMigration complete. New columns added.")
    else:
        print("\nNo missing columns found. Database is up to date.")

    conn.close()

if __name__ == "__main__":
    migrate()
