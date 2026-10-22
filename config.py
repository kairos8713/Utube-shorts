"""
config.py — single source of runtime settings.

Every secret and tunable comes from the environment / `.env` (python-dotenv).
Values are read lazily on attribute access so tests can monkeypatch os.environ.

    from config import settings
    if settings.DRY_RUN: ...

Hard safety limits do NOT live here — they are in guardrails.py and are not
overridable from .env.
"""
import os

from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(ROOT_DIR, ".env")

if not os.environ.get("REELS_NO_DOTENV"):      # tests set this to stay hermetic
    load_dotenv(ENV_PATH, override=False)


def _bool(raw: str) -> bool:
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


# name -> (type, default). Defaults are safe: DRY_RUN on, panel on localhost,
# e-mail in test (outbox) mode.
_SPEC = {
    # ── core ────────────────────────────────────────────────────────────────
    "DRY_RUN":               (_bool, "true"),
    "DATABASE_URL":          (str, "sqlite:///" + os.path.join(ROOT_DIR, "automation.db").replace("\\", "/")),
    "DISPLAY_TZ":            (str, "Europe/Warsaw"),
    "LOG_DIR":               (str, os.path.join(ROOT_DIR, "logs")),
    "WORK_DIR":              (str, os.path.join(ROOT_DIR, "work")),
    "BACKUP_DIR":            (str, os.path.join(ROOT_DIR, "backups")),
    "REELS_DIR":             (str, os.path.join(ROOT_DIR, "reels")),
    "DOWNLOAD_DIR":          (str, os.path.join(ROOT_DIR, "downloads")),
    "SEGMENTS_DIR":          (str, os.path.join(ROOT_DIR, "segments")),
    "COOKIES_PATH":          (str, os.path.join(ROOT_DIR, "cookies.txt")),
    "NODE_PATH":             (str, r"C:\Program Files\nodejs\node.exe"),
    # ── secrets ─────────────────────────────────────────────────────────────
    "FERNET_KEY":            (str, ""),
    # ── panel ───────────────────────────────────────────────────────────────
    "PANEL_HOST":            (str, "127.0.0.1"),
    "PANEL_PORT":            (int, "5050"),
    "PANEL_USERNAME":        (str, "emre"),
    "PANEL_PASSWORD_HASH":   (str, ""),
    "PANEL_SECRET_KEY":      (str, ""),
    # ── GPU / LLM ───────────────────────────────────────────────────────────
    "GPU_LOCK_PATH":         (str, os.path.join(os.path.expanduser("~"), "gpu_shared.lock")),
    "GPU_LOCK_TIMEOUT_S":    (int, "21600"),
    "OLLAMA_URL":            (str, "http://127.0.0.1:11434"),
    "OLLAMA_MODEL":          (str, "qwen3:32b"),      # `ollama pull qwen3:32b`; alt: qwen3:30b-a3b
    "OLLAMA_TIMEOUT_S":      (int, "600"),
    "OLLAMA_NUM_CTX":        (int, "6144"),           # 2x RTX 3060: qwen3:32b spills to CPU at 8192 (measured)
    "OLLAMA_NUM_GPU":        (str, ""),               # empty = Ollama decides; e.g. 99 forces all layers on GPU
    "OLLAMA_KEEP_ALIVE":     (str, "15m"),           # within a batch; unload() → keep_alive 0
    "LLM_BACKEND":           (str, "ollama"),        # ollama | mock (tests / E2E without Ollama)
    "WHISPER_MODEL":         (str, "small"),
    # ── Instagram ───────────────────────────────────────────────────────────
    "IG_APP_ID":             (str, ""),
    "IG_APP_SECRET":         (str, ""),
    "IG_REDIRECT_URI":       (str, "https://localhost:5050/oauth/instagram/callback"),
    "IG_API_VERSION":        (str, "v25.0"),          # verified 2026-10-19; v26.0 is newest Graph version
    "IG_UPLOAD_METHOD":      (str, "r2_url"),         # r2_url | resumable (resumable = Facebook Login only per docs)
    "IG_POLL_TIMEOUT_S":     (int, "600"),
    # ── R2 / S3 temporary hosting (IG_UPLOAD_METHOD=r2_url) ─────────────────
    "R2_ENDPOINT_URL":       (str, ""),
    "R2_ACCESS_KEY_ID":      (str, ""),
    "R2_SECRET_ACCESS_KEY":  (str, ""),
    "R2_BUCKET":             (str, ""),
    "R2_PUBLIC_BASE_URL":    (str, ""),              # only needed when R2_URL_MODE=public
    "R2_URL_MODE":           (str, "presigned"),     # presigned (1 h signed GET URL) | public
    "R2_PRESIGN_EXPIRES_S":  (int, "3600"),
    # ── e-mail ──────────────────────────────────────────────────────────────
    "EMAIL_ENABLED":         (_bool, "false"),
    "EMAIL_TEST_MODE":       (_bool, "true"),         # true → write to logs/outbox/
    "SMTP_HOST":             (str, "smtp.gmail.com"),
    "SMTP_PORT":             (int, "587"),
    "SMTP_USER":             (str, ""),
    "SMTP_PASSWORD":         (str, ""),
    "EMAIL_FROM":            (str, ""),
    "EMAIL_TO":              (str, ""),
    # ── render ──────────────────────────────────────────────────────────────
    "BG_MUSIC_ENABLED":      (_bool, "false"),        # bg_music tracks may not be licensed for Instagram
    # ── YouTube / yt-dlp ────────────────────────────────────────────────────
    # Verified working on this PC 2026-10-22 (docs/YTDLP_FIX.md): yt-dlp 2026.10.21 in .venv +
    # yt-dlp-ejs + deno, yt-dlp's DEFAULT player clients, no cookies, no PO token needed.
    "YTDLP_PYTHON":          (str, ""),               # empty → .venv\Scripts\python.exe (else current python)
    "YTDLP_PLAYER_CLIENT":   (str, ""),               # empty = yt-dlp defaults; fallback idea: "mweb" + PO server
    "YTDLP_JS_RUNTIME":      (str, "deno"),           # deno (EJS wiki recommendation) | node | "" (auto)
    "YTDLP_JS_RUNTIME_PATH": (str, ""),               # empty → tools\deno\deno.exe / NODE_PATH
    "YTDLP_REMOTE_COMPONENTS": (str, ""),             # empty = solver from the yt-dlp-ejs pip package
    "YTDLP_USE_COOKIES":     (_bool, "false"),        # account cookies only if needed (ban risk)
    "YTDLP_UPDATE_CHANNEL":  (str, "stable"),         # stable | nightly (pip --pre)
    "POT_PROVIDER_URL":      (str, ""),               # bgutil HTTP server; empty = default 127.0.0.1:4416
    "OLLAMA_EXE":            (str, ""),               # empty → %LOCALAPPDATA%\Programs\Ollama\ollama.exe
    # ── scheduling ──────────────────────────────────────────────────────────
    "POSTING_WINDOW_START":  (str, "15:00"),          # Europe/Warsaw local time
    "POSTING_WINDOW_END":    (str, "05:00"),
    # ── ops ─────────────────────────────────────────────────────────────────
    "DISK_ALERT_PCT":        (float, "10"),
    "COOKIES_MAX_AGE_DAYS":  (int, "14"),
}


class _Settings:
    def __getattr__(self, name):
        if name not in _SPEC:
            raise AttributeError(f"Unknown setting: {name}")
        cast, default = _SPEC[name]
        raw = os.environ.get(name)
        if raw is None or raw == "":
            raw = default
        return cast(raw)

    def names(self):
        return list(_SPEC)


settings = _Settings()


def env_example_text() -> str:
    """Render .env.example with every key and empty secret values."""
    secret_like = ("KEY", "SECRET", "PASSWORD", "HASH", "TOKEN")
    lines = ["# Copy to .env and fill in. Generated from config.py — keep in sync.", ""]
    for name, (_, default) in _SPEC.items():
        value = "" if any(s in name for s in secret_like) else default
        if name in ("DATABASE_URL", "LOG_DIR", "WORK_DIR", "BACKUP_DIR", "REELS_DIR",
                    "DOWNLOAD_DIR", "SEGMENTS_DIR", "COOKIES_PATH", "GPU_LOCK_PATH"):
            value = ""          # machine-specific, defaults are computed
        lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"
