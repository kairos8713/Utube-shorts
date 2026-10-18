"""
init_secrets.py — create/complete the local .env.

  python scripts/init_secrets.py                      # create .env, generate missing keys
  python scripts/init_secrets.py --set-panel-password # prompt for the panel password (hashed)
  python scripts/init_secrets.py --write-example      # regenerate .env.example from config.py

Never overwrites an existing FERNET_KEY: rotating it would make every stored
token/password unreadable.
"""
import argparse
import getpass
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import ENV_PATH, ROOT_DIR, env_example_text  # noqa: E402


def read_env(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return f.read().splitlines()


def get_value(lines: list, key: str) -> str:
    for line in lines:
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def set_value(lines: list, key: str, value: str, quote: bool = False) -> list:
    rendered = f"{key}='{value}'" if quote else f"{key}={value}"
    out, done = [], False
    for line in lines:
        if line.startswith(key + "="):
            out.append(rendered)
            done = True
        else:
            out.append(line)
    if not done:
        out.append(rendered)
    return out


def write_env(path: str, lines: list):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def ensure_env(path: str = ENV_PATH) -> list:
    from crypto_utils import generate_key

    lines = read_env(path)
    if not lines:
        lines = env_example_text().splitlines()
    changed = []
    if not get_value(lines, "FERNET_KEY"):
        lines = set_value(lines, "FERNET_KEY", generate_key())
        changed.append("FERNET_KEY")
    if not get_value(lines, "PANEL_SECRET_KEY"):
        lines = set_value(lines, "PANEL_SECRET_KEY", secrets.token_hex(32))
        changed.append("PANEL_SECRET_KEY")
    lines, imported = import_legacy_email(lines)
    changed += imported
    write_env(path, lines)
    return changed


def import_legacy_email(lines: list, legacy_path: str = None) -> tuple:
    """Copy SMTP settings from the old email_config.json into .env (once)."""
    import json

    legacy_path = legacy_path or os.path.join(ROOT_DIR, "email_config.json")
    if get_value(lines, "SMTP_PASSWORD") or not os.path.exists(legacy_path):
        return lines, []
    try:
        with open(legacy_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return lines, []
    mapping = {
        "SMTP_HOST": cfg.get("smtp_host"),
        "SMTP_PORT": cfg.get("smtp_port"),
        "SMTP_USER": cfg.get("from_email"),
        "SMTP_PASSWORD": cfg.get("smtp_password"),
        "EMAIL_FROM": cfg.get("from_email"),
        "EMAIL_TO": cfg.get("to_email"),
    }
    imported = []
    for key, val in mapping.items():
        if val and not get_value(lines, key):
            lines = set_value(lines, key, str(val), quote=(key == "SMTP_PASSWORD"))
            imported.append(key)
    return lines, imported


def set_panel_password(path: str = ENV_PATH, password: str = None):
    from werkzeug.security import generate_password_hash

    if password is None:
        password = getpass.getpass("New panel password: ")
        again = getpass.getpass("Repeat: ")
        if password != again:
            sys.exit("Passwords do not match.")
    if len(password) < 10:
        sys.exit("Use at least 10 characters.")
    lines = read_env(path)
    lines = set_value(lines, "PANEL_PASSWORD_HASH", generate_password_hash(password), quote=True)
    write_env(path, lines)
    print("PANEL_PASSWORD_HASH updated.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set-panel-password", action="store_true")
    ap.add_argument("--write-example", action="store_true")
    args = ap.parse_args()

    if args.write_example:
        with open(os.path.join(ROOT_DIR, ".env.example"), "w", encoding="utf-8") as f:
            f.write(env_example_text())
        print(".env.example written.")
        return
    changed = ensure_env()
    print(f".env ready at {ENV_PATH}. Generated: {changed or 'nothing (already set)'}")
    if args.set_panel_password:
        set_panel_password()


if __name__ == "__main__":
    main()
