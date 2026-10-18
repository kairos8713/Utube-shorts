import os
import sqlite3

import pytest
from werkzeug.security import generate_password_hash


# ── crypto ───────────────────────────────────────────────────────────────────

def test_encrypt_roundtrip_and_prefix():
    from crypto_utils import decrypt, encrypt, is_encrypted
    c = encrypt("hunter2")
    assert is_encrypted(c) and "hunter2" not in c
    assert decrypt(c) == "hunter2"
    assert encrypt(c) == c                      # idempotent
    assert decrypt("legacy-plain") == "legacy-plain"


def test_encrypt_without_key_fails(monkeypatch):
    from crypto_utils import CryptoNotConfigured, encrypt
    monkeypatch.setenv("FERNET_KEY", "")
    with pytest.raises(CryptoNotConfigured):
        encrypt("x")


# ── migration: plaintext passwords → Fernet, with backup ─────────────────────

def _make_legacy_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE owned_channels (id INTEGER PRIMARY KEY, password VARCHAR NOT NULL)")
    conn.execute("INSERT INTO owned_channels VALUES (1, 'plain-pass'), (2, 'other')")
    conn.commit()
    conn.close()


def test_password_migration_encrypts_and_backs_up(tmp_path):
    import db_migrate
    from crypto_utils import decrypt, is_encrypted

    path = str(tmp_path / "legacy.db")
    _make_legacy_db(path)
    applied = db_migrate.run(path, log=lambda *_: None)
    assert "001_encrypt_owned_passwords" in applied

    conn = sqlite3.connect(path)
    rows = dict(conn.execute("SELECT id, password FROM owned_channels").fetchall())
    conn.close()
    assert all(is_encrypted(v) for v in rows.values())
    assert decrypt(rows[1]) == "plain-pass"

    backups = os.listdir(os.path.join(os.environ["BACKUP_DIR"], "migrations"))
    assert any("before_001" in b for b in backups)

    # Idempotent: second run applies nothing and ciphertexts are unchanged.
    assert db_migrate.run(path, log=lambda *_: None) == []
    conn = sqlite3.connect(path)
    rows2 = dict(conn.execute("SELECT id, password FROM owned_channels").fetchall())
    conn.close()
    assert rows2 == rows


# ── init_secrets ─────────────────────────────────────────────────────────────

def test_init_secrets_generates_and_never_rotates_fernet(tmp_path):
    from scripts import init_secrets
    env = str(tmp_path / ".env")
    changed = init_secrets.ensure_env(env)
    assert "FERNET_KEY" in changed and "PANEL_SECRET_KEY" in changed
    first = init_secrets.get_value(init_secrets.read_env(env), "FERNET_KEY")
    assert init_secrets.ensure_env(env) == []
    assert init_secrets.get_value(init_secrets.read_env(env), "FERNET_KEY") == first


def test_init_secrets_password_hash_is_quoted(tmp_path):
    from dotenv import dotenv_values
    from scripts import init_secrets
    env = str(tmp_path / ".env")
    init_secrets.ensure_env(env)
    init_secrets.set_panel_password(env, password="correct horse battery")
    from werkzeug.security import check_password_hash
    stored = dotenv_values(env)["PANEL_PASSWORD_HASH"]
    assert check_password_hash(stored, "correct horse battery")


# ── panel auth ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from web import auth, create_app
    auth.reset_rate_limit()
    monkeypatch.setenv("PANEL_USERNAME", "emre")
    monkeypatch.setenv("PANEL_PASSWORD_HASH", generate_password_hash("s3cret-pass!"))
    app = create_app(testing=True)
    return app.test_client()


def test_panel_defaults_to_localhost():
    from config import settings
    assert settings.PANEL_HOST == "127.0.0.1"


def test_login_disabled_without_hash(monkeypatch):
    from web import auth, create_app
    auth.reset_rate_limit()
    monkeypatch.setenv("PANEL_PASSWORD_HASH", "")
    c = create_app(testing=True).test_client()
    r = c.post("/login", data={"username": "emre", "password": ""})
    assert r.status_code == 200 and "ayarlanmamış".encode() in r.data
    assert c.get("/").status_code == 302


def test_old_hardcoded_root_toor_rejected(client):
    r = client.post("/login", data={"username": "root", "password": "toor"})
    assert r.status_code == 200
    assert client.get("/").status_code == 302


def test_login_success(client):
    r = client.post("/login", data={"username": "emre", "password": "s3cret-pass!"})
    assert r.status_code == 302
    assert client.get("/").status_code == 200


def test_open_redirect_blocked(client):
    r = client.post("/login?next=//evil.example", data={"username": "emre", "password": "s3cret-pass!"})
    assert r.headers["Location"] == "/"


def test_rate_limit_locks_out(client):
    for _ in range(5):
        client.post("/login", data={"username": "emre", "password": "wrong"})
    r = client.post("/login", data={"username": "emre", "password": "s3cret-pass!"})
    assert r.status_code == 429


def test_secret_key_from_env():
    from web import create_app
    assert create_app(testing=True).secret_key == "test-secret"
