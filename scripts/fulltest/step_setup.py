"""Step 2: create test_runs/full_<ts>/, copy the real DB (backup API), prepare test accounts."""
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

REAL_DB = os.path.join(common.ROOT, "automation.db")
NICHES = [  # 4 niches of the original project (bg_music folders), clean names for the prompts
    ("Podcast", "podcast_riskli"),
    ("Personal Finance", "personal_finance_riskli"),
    ("Tech / AI", "tech_ai_riskli"),
    ("Psychology", "psychology_riskli"),
]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    run = os.path.join(common.ROOT, "test_runs", f"full_{ts}")
    os.makedirs(run, exist_ok=True)
    with open(common.CURRENT, "w", encoding="utf-8") as f:
        f.write(run)
    real = {"sha256": sha256(REAL_DB), "size": os.path.getsize(REAL_DB),
            "wal_exists": os.path.exists(REAL_DB + "-wal"), "shm_exists": os.path.exists(REAL_DB + "-shm"),
            "mtime": os.path.getmtime(REAL_DB)}
    copy = os.path.join(run, "automation_copy.db")
    src = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    dst = sqlite3.connect(copy)
    src.backup(dst)
    dst.close()
    src.close()

    common.activate(run)
    import db_migrate
    import models
    from crypto_utils import encrypt
    from models import OwnedChannels, Sectors, Source
    applied = db_migrate.run(copy, log=lambda *_: None)

    s = models.Session()
    now = dt.datetime.utcnow()
    paused = []
    for acc in s.query(OwnedChannels).all():          # real accounts: never used by the test
        acc.status, acc.pause_reason = "PAUSED", "full test: real account excluded"
        paused.append(acc.id)
    blacklisted = []
    for src_row in s.query(Source).filter(Source.channel_url.like("%@MrBeast%")).all():
        src_row.status, src_row.status_reason = "BLACKLISTED", "Emre's decision (full test copy only)"
        src_row.status_changed_at = now
        blacklisted.append(src_row.channel_url)
    accounts = []
    for n, (name, slug) in enumerate(NICHES, start=1):
        sec = s.query(Sectors).filter_by(name=name).first() or Sectors(
            name=name, title=name, description=name, tags=name, music_slug=slug)
        s.add(sec)
        s.flush()
        acc = OwnedChannels(
            name=f"test_{slug.replace('_riskli', '')}", ig_username=f"test_{slug.replace('_riskli', '')}",
            sector_id=sec.id, level=1, mail="-", password=encrypt("-"), status="ACTIVE", daily_post_cap=1,
            ig_user_id=f"1784100000000000{n}", ig_access_token=encrypt(f"DRY-RUN-TOKEN-{n}"),
            ig_token_expires_at=now + dt.timedelta(days=50), ig_token_refreshed_at=now - dt.timedelta(days=5),
            ig_setup_completed_at=now - dt.timedelta(days=3), activated_at=now - dt.timedelta(days=1),
            consecutive_failures=0)
        s.add(acc)
        s.flush()
        accounts.append({"id": acc.id, "name": acc.name, "niche": name, "sector_id": sec.id})
    s.commit()

    budget = common.Budget()
    if not budget.used():
        budget.take("1c probe: TcMBFSGVi1c (scripts/ytdlp_probe.py)")

    # network guard self-test (the record is removed afterwards so the violations file stays clean)
    import requests
    try:
        requests.get("https://graph.instagram.com/v25.0/me", timeout=5)
        guard = "NOT BLOCKED"
    except RuntimeError as e:
        guard = f"blocked: {e}"
    vpath = os.path.join(run, "network_violations.jsonl")
    if os.path.exists(vpath):
        os.remove(vpath)

    from config import settings
    out = {"run_dir": run, "real_db_before": real, "migrations_applied_to_copy": applied,
           "real_accounts_paused_in_copy": paused, "blacklisted_in_copy": blacklisted, "test_accounts": accounts,
           "settings": {"DRY_RUN": settings.DRY_RUN, "EMAIL_TEST_MODE": settings.EMAIL_TEST_MODE,
                        "DATABASE_URL": settings.DATABASE_URL, "OLLAMA_MODEL": settings.OLLAMA_MODEL,
                        "OLLAMA_NUM_CTX": settings.OLLAMA_NUM_CTX, "OLLAMA_NUM_GPU": settings.OLLAMA_NUM_GPU,
                        "IG_UPLOAD_METHOD": settings.IG_UPLOAD_METHOD, "R2_ENDPOINT_URL": settings.R2_ENDPOINT_URL},
           "network_guard_selftest": guard, "resources": common.sample_resources("setup")}
    common.save_result("setup", out)
    print(json.dumps({k: out[k] for k in ("run_dir", "real_accounts_paused_in_copy", "blacklisted_in_copy",
                                          "test_accounts", "settings", "network_guard_selftest")},
                     indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
