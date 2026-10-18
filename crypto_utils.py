"""
crypto_utils.py — Fernet encryption for secrets stored in the DB
(account passwords, Instagram access tokens).

Ciphertexts carry an "enc:v1:" prefix so plaintext legacy values can be
detected and migrated idempotently. The key lives in .env as FERNET_KEY
(create it with scripts/init_secrets.py). Losing the key = losing every
stored token/password, so it is also included in the daily backup notes.
"""
from cryptography.fernet import Fernet, InvalidToken

from config import settings

PREFIX = "enc:v1:"


class CryptoNotConfigured(RuntimeError):
    pass


def generate_key() -> str:
    return Fernet.generate_key().decode()


def _fernet() -> Fernet:
    key = settings.FERNET_KEY
    if not key:
        raise CryptoNotConfigured("FERNET_KEY is not set — run scripts/init_secrets.py")
    return Fernet(key.encode())


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(plaintext) -> str:
    if plaintext is None:
        return None
    if is_encrypted(plaintext):
        return plaintext
    return PREFIX + _fernet().encrypt(str(plaintext).encode("utf-8")).decode()


def decrypt(value) -> str:
    """Decrypt an enc:v1 value. Plaintext (legacy) values are returned unchanged."""
    if value is None:
        return None
    if not is_encrypted(value):
        return value
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode()).decode("utf-8")
    except InvalidToken as e:
        raise CryptoNotConfigured("FERNET_KEY does not match stored ciphertext") from e
