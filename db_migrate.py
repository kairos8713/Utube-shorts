"""
db_migrate.py — idempotent, backed-up schema/data migrations (SQLite).

    python db_migrate.py            # apply pending migrations (backs up first)
    python db_migrate.py --status

Rules:
  * Every migration is idempotent (safe to re-run) and recorded in
    `schema_migrations`.
  * Before applying ANY pending migration the DB file is copied to
    backups/migrations/<utc-ts>_before_<name>.db. No backup → no migration.
  * Old YouTube-era columns are never dropped; we just stop using them.
"""
import datetime
import os
import shutil
import sqlite3
import sys

from config import settings


def sqlite_path(url: str = None) -> str:
    url = url or settings.DATABASE_URL
    if not url.startswith("sqlite:///"):
        raise ValueError(f"Only sqlite is supported, got {url}")
    return url[len("sqlite:///"):]


def _columns(conn, table) -> set:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _table_exists(conn, table) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def add_column(conn, table, column, ddl):
    if _table_exists(conn, table) and column not in _columns(conn, table):
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {column} {ddl}')


# ── migrations ───────────────────────────────────────────────────────────────

def m001_encrypt_owned_passwords(conn):
    """Encrypt plaintext owned_channels.password values with Fernet."""
    from crypto_utils import encrypt, is_encrypted

    if not _table_exists(conn, "owned_channels"):
        return
    rows = conn.execute("SELECT id, password FROM owned_channels").fetchall()
    for row_id, pw in rows:
        if pw and not is_encrypted(pw):
            conn.execute("UPDATE owned_channels SET password=? WHERE id=?", (encrypt(pw), row_id))


def m002_instagram_schema(conn):
    """Instagram fields on owned_channels + video bookkeeping columns."""
    oc = [
        ("ig_user_id", "VARCHAR"),
        ("ig_username", "VARCHAR"),
        ("ig_access_token", "VARCHAR"),          # enc:v1 Fernet ciphertext
        ("ig_token_expires_at", "DATETIME"),
        ("ig_token_refreshed_at", "DATETIME"),
        ("ig_setup_completed_at", "DATETIME"),
        ("status", "VARCHAR DEFAULT 'WARMUP'"),
        ("pause_reason", "VARCHAR"),
        ("paused_at", "DATETIME"),
        ("daily_post_cap", "INTEGER DEFAULT 1"),
        ("posting_window", "VARCHAR"),           # "15:00-05:00" local, NULL = default
        ("activated_at", "DATETIME"),
        ("consecutive_failures", "INTEGER DEFAULT 0"),
        ("niche", "VARCHAR"),
    ]
    for col, ddl in oc:
        add_column(conn, "owned_channels", col, ddl)

    vid = [
        ("source_id", "INTEGER"),
        ("source_url", "VARCHAR"),
        ("segment_start", "FLOAT"),
        ("segment_end", "FLOAT"),
        ("phash", "VARCHAR"),
        ("file_path", "VARCHAR"),
        ("cover_path", "VARCHAR"),
        ("cover_offset_ms", "INTEGER"),
        ("transcript", "VARCHAR"),
        ("qc_status", "VARCHAR"),
        ("qc_report", "VARCHAR"),
        ("cycle_id", "INTEGER"),
        ("time_sensitive", "BOOLEAN DEFAULT 0"),
        ("clip_duration", "FLOAT"),
    ]
    for col, ddl in vid:
        add_column(conn, "videos", col, ddl)

    uq = [
        ("slot_source", "VARCHAR"),
        ("priority", "INTEGER DEFAULT 0"),
        ("post_id", "INTEGER"),
    ]
    for col, ddl in uq:
        add_column(conn, "upload_queue", col, ddl)

    # Existing rows: keep active accounts in WARMUP until Emre enters
    # ig_setup_completed_at in the panel (warm-up gate).
    conn.execute("UPDATE owned_channels SET status='WARMUP' WHERE status IS NULL")
    conn.execute("UPDATE owned_channels SET daily_post_cap=1 WHERE daily_post_cap IS NULL")


def m003_sector_niche_slug(conn):
    """sectors.music_slug → bg_music/<slug>/ ; filled when a folder matches the name."""
    from media_utils import available_music_slugs, music_slug

    if not _table_exists(conn, "sectors"):
        return
    add_column(conn, "sectors", "music_slug", "VARCHAR")
    folders = set(available_music_slugs())
    aliases = {"tech__ai_riskli": "tech_ai_riskli", "food__cooking_riskli": "food_cooking_riskli",
               "automative_riskli": "automotive_riskli"}
    for sid, name, current in conn.execute("SELECT id, name, music_slug FROM sectors").fetchall():
        if current:
            continue
        slug = aliases.get(music_slug(name), music_slug(name))
        if slug in folders:
            conn.execute("UPDATE sectors SET music_slug=? WHERE id=?", (slug, sid))


MIGRATIONS = [
    ("001_encrypt_owned_passwords", m001_encrypt_owned_passwords),
    ("003_sector_music_slug", m003_sector_niche_slug),
]


# ── runner ───────────────────────────────────────────────────────────────────

def _ensure_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " name VARCHAR PRIMARY KEY, applied_at DATETIME NOT NULL)"
    )


def applied(conn) -> set:
    _ensure_table(conn)
    return {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}


def pending(db_path: str = None) -> list:
    db_path = db_path or sqlite_path()
    conn = sqlite3.connect(db_path)
    try:
        done = applied(conn)
        return [name for name, _ in MIGRATIONS if name not in done]
    finally:
        conn.close()


def backup_db(db_path: str, label: str) -> str:
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    dst_dir = os.path.join(settings.BACKUP_DIR, "migrations")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, f"{ts}_before_{label}.db")
    src = sqlite3.connect(db_path)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)          # consistent online copy
    finally:
        out.close()
        src.close()
    if not os.path.exists(dst) or os.path.getsize(dst) == 0:
        raise RuntimeError("DB backup failed — refusing to migrate")
    return dst


def run(db_path: str = None, log=print) -> list:
    db_path = db_path or sqlite_path()
    todo = pending(db_path)
    if not todo:
        return []
    if os.path.exists(db_path) and os.path.getsize(db_path) > 0:
        dst = backup_db(db_path, todo[0])
        log(f"[MIGRATE] backup → {dst}")
    conn = sqlite3.connect(db_path)
    try:
        for name, fn in MIGRATIONS:
            if name not in todo:
                continue
            with conn:  # one transaction per migration
                fn(conn)
                conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                    (name, datetime.datetime.utcnow().isoformat(sep=" ")),
                )
            log(f"[MIGRATE] applied {name}")
    finally:
        conn.close()
    return todo


if __name__ == "__main__":
    if "--status" in sys.argv:
        print("pending:", pending())
    else:
        # Import models first so SQLAlchemy creates any brand-new tables.
        import models  # noqa: F401
        print("applied:", run())
