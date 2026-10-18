"""
Hermetic test environment: temp DB/log/work dirs, DRY_RUN on, e-mail to outbox,
real .env never loaded. Must run before any project import.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="reels_test_")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.update({
    "REELS_NO_DOTENV": "1",
    "DATABASE_URL": "sqlite:///" + os.path.join(_TMP, "test.db").replace("\\", "/"),
    "LOG_DIR": os.path.join(_TMP, "logs"),
    "WORK_DIR": os.path.join(_TMP, "work"),
    "BACKUP_DIR": os.path.join(_TMP, "backups"),
    "REELS_DIR": os.path.join(_TMP, "reels"),
    "DOWNLOAD_DIR": os.path.join(_TMP, "downloads"),
    "SEGMENTS_DIR": os.path.join(_TMP, "segments"),
    "GPU_LOCK_PATH": os.path.join(_TMP, "gpu.lock"),
    "DRY_RUN": "true",
    "EMAIL_ENABLED": "true",
    "EMAIL_TEST_MODE": "true",
    "FERNET_KEY": Fernet.generate_key().decode(),
    "PANEL_SECRET_KEY": "test-secret",
    "PANEL_PASSWORD_HASH": "",
    "IG_APP_ID": "test-app",
    "IG_APP_SECRET": "test-secret",
    "SMTP_PASSWORD": "",
})

import pytest  # noqa: E402


@pytest.fixture
def tmp_root():
    return _TMP


@pytest.fixture
def db():
    """Fresh schema for each test; yields a SQLAlchemy session."""
    import models
    models.Base.metadata.drop_all(models.engine)
    models.Base.metadata.create_all(models.engine)
    sess = models.Session()
    try:
        yield sess
    finally:
        sess.close()
