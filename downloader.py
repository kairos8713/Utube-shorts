"""
downloader.py — yt-dlp wrappers (download + flat metadata), YouTube health.

* Bot-check ("sign in to confirm you're not a bot") / cookie errors are raised as YouTubeBlocked at
  once, so the cycle stops all downloads and raises an alert instead of hammering YouTube.
* A 403 (or 429) on ONE video is not a block (full test: the same video's audio had downloaded fine):
  wait 60–120 s, retry once with an avc1 format; still 403 → only that video fails (VideoForbidden).
  YouTubeBlocked("FORBIDDEN_403_REPEATED") only after ≥ 3 DIFFERENT videos failed so within 1 hour.
* polite_delay() spreads downloads out in time.
* yt-dlp self-update runs at most once per cycle (update_ytdlp_once()).
"""
import json
import os
import random
import re
import subprocess
import sys
import time

from config import ROOT_DIR, settings
from log_setup import get_logger

log = get_logger("downloader")

BOT_HINTS = (
    "sign in to confirm you", "not a bot", "confirm you're not a bot",
)
RATE_HINTS = ("http error 429", "too many requests")
FORBIDDEN_RETRY_FORMAT = "bv*[vcodec^=avc1][height<=1080]+ba/b"
FORBIDDEN_RETRY_WAIT_S = (60, 120)
STRIKE_WINDOW_S = 3600
STRIKES_TO_BLOCK = 3
_STRIKES_KEY = "yt_forbidden_strikes"
_sleep = time.sleep                     # tests replace it
COOKIE_HINTS = (
    "cookies are no longer valid", "the provided youtube account cookies",
    "cookies file", "invalid cookies", "use --cookies",
)
PRIVATE_HINTS = (
    "members only", "private video", "video is private", "join this channel",
    "subscribers only", "this video is available to", "login required",
)


class DownloadError(RuntimeError):
    pass


class YouTubeBlocked(DownloadError):
    """Bot-check or cookie failure: stop all downloads, alert Emre."""

    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail[:300]}")
        self.kind = kind


class VideoUnavailable(DownloadError):
    pass


class VideoForbidden(DownloadError):
    """403 / 429 on one video: that video fails; repeated on different videos → YouTubeBlocked."""

    def __init__(self, detail: str, kind: str = "FORBIDDEN_403"):
        super().__init__(f"{kind}: {detail[:300]}")
        self.kind = kind


def classify_ytdlp_error(stderr: str):
    s = (stderr or "").lower()
    if any(h in s for h in BOT_HINTS):
        return YouTubeBlocked("BOT_CHECK", stderr)
    if any(h in s for h in COOKIE_HINTS):
        return YouTubeBlocked("COOKIES", stderr)
    if "http error 403" in s and ("po token" in s or "unable to download video data" in s):
        return VideoForbidden(stderr, "FORBIDDEN_403")
    if any(h in s for h in RATE_HINTS):
        return VideoForbidden(stderr, "RATE_LIMITED_429")
    if any(h in s for h in PRIVATE_HINTS):
        return VideoUnavailable(stderr[:300])
    return DownloadError(stderr[-500:] if stderr else "yt-dlp failed")


BLOCKED_HINTS = {
    "BOT_CHECK": "YouTube bot check — pause downloads for a while and check the IP (docs/YTDLP_FIX.md).",
    "COOKIES": "cookie failure — the working set-up runs WITHOUT cookies: clear YTDLP_COOKIES / cookies.txt "
               "(docs/YTDLP_FIX.md).",
    "FORBIDDEN_403_PO_TOKEN": "403 / PO token — update yt-dlp + yt-dlp-ejs in .venv and check deno and the "
                              "PO-token provider (scripts/setup_venv.ps1, docs/YTDLP_FIX.md). Cookies do not help.",
    "FORBIDDEN_403_REPEATED": "403/429 on 3+ different videos within 1 hour — update yt-dlp + yt-dlp-ejs in .venv, "
                              "check deno and the PO-token provider and the IP (docs/YTDLP_FIX.md). Cookies do not help.",
}


def blocked_hint(kind: str) -> str:
    """What Emre should do for a YouTubeBlocked kind (alert / e-mail text)."""
    return BLOCKED_HINTS.get(kind, "see docs/YTDLP_FIX.md")


def cookies_age_days() -> float:
    path = settings.COOKIES_PATH
    if not os.path.exists(path):
        return float("inf")
    return (time.time() - os.path.getmtime(path)) / 86400.0


def polite_delay(min_s: float = 20, max_s: float = 90, sleep=time.sleep):
    d = random.uniform(min_s, max_s)
    log.info("polite delay %.0fs before next YouTube request", d)
    sleep(d)


_updated_this_process = False


def ytdlp_python() -> str:
    """Interpreter whose yt-dlp we run: YTDLP_PYTHON, else the project .venv, else the current one.
    Keeps services on the venv's (frequently updated) yt-dlp even if started with another Python."""
    if settings.YTDLP_PYTHON:
        return settings.YTDLP_PYTHON
    venv = os.path.join(ROOT_DIR, ".venv", "Scripts", "python.exe")
    return venv if os.path.exists(venv) else sys.executable


def js_runtime_arg():
    """`--js-runtimes` value for the YouTube JS challenge solver (EJS), with a FULL path —
    PATH on this PC is incomplete. EJS wiki: deno recommended (>= 2.3), node >= 22 supported."""
    rt = (settings.YTDLP_JS_RUNTIME or "").strip().lower()
    if not rt:
        return None
    path = settings.YTDLP_JS_RUNTIME_PATH
    if not path:
        if rt == "deno":
            for cand in (os.path.join(ROOT_DIR, "tools", "deno", "deno.exe"),
                         os.path.join(os.path.expanduser("~"), ".deno", "bin", "deno.exe")):
                if os.path.exists(cand):
                    path = cand
                    break
        elif rt == "node":
            path = settings.NODE_PATH
    return f"{rt}:{path}" if path and os.path.exists(path) else rt


def update_ytdlp_once():
    """Once per cycle: newest yt-dlp (+ EJS solver, + PO-token plugin) in the yt-dlp interpreter."""
    global _updated_this_process
    if _updated_this_process:
        return
    _updated_this_process = True
    cmd = [ytdlp_python(), "-m", "pip", "install", "-U"]
    if settings.YTDLP_UPDATE_CHANNEL == "nightly":
        cmd.append("--pre")
    cmd += ["yt-dlp[default]", "bgutil-ytdlp-pot-provider"]
    log.info("Updating yt-dlp (once per cycle): %s", " ".join(cmd[1:]))
    r = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        log.warning("yt-dlp update failed: %s", (r.stderr or "")[-500:])


def _base_args() -> list:
    """Shared by every yt-dlp call: downloads, ytsearch, channel lists, latest uploads, audio samples."""
    args = [ytdlp_python(), "-m", "yt_dlp"]
    rt = js_runtime_arg()
    if rt:
        args += ["--js-runtimes", rt]
    if settings.YTDLP_REMOTE_COMPONENTS:
        args += ["--remote-components", settings.YTDLP_REMOTE_COMPONENTS]
    if settings.YTDLP_PLAYER_CLIENT:
        args += ["--extractor-args", f"youtube:player_client={settings.YTDLP_PLAYER_CLIENT}"]
    if settings.YTDLP_USE_COOKIES and os.path.exists(settings.COOKIES_PATH):
        args += ["--cookies", settings.COOKIES_PATH]
    if settings.POT_PROVIDER_URL:      # bgutil PO-token server on a non-default address
        args += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={settings.POT_PROVIDER_URL}"]
    return args


def ytdlp_json(target: str, extra: list = None, timeout: int = 120) -> dict:
    """`yt-dlp --dump-single-json` for channels/searches/videos (no download)."""
    cmd = _base_args() + ["--dump-single-json", "--no-warnings"] + (extra or []) + [target]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        raise classify_ytdlp_error(r.stderr)
    return json.loads(r.stdout or "{}")


def record_forbidden(video_id: str, now: float = None) -> int:
    """Remember a 403/429 failure; returns how many DIFFERENT videos failed so in the last hour."""
    import system_state
    now = now or time.time()
    try:
        strikes = json.loads(system_state.get(_STRIKES_KEY, "[]") or "[]")
    except ValueError:
        strikes = []
    strikes = [x for x in strikes if now - x.get("at", 0) < STRIKE_WINDOW_S and x.get("video") != video_id]
    strikes.append({"video": video_id, "at": now})
    system_state.put(_STRIKES_KEY, json.dumps(strikes))
    return len({x["video"] for x in strikes})


def _forbidden_failure(video_id: str, err: "VideoForbidden"):
    n = record_forbidden(video_id)
    if n >= STRIKES_TO_BLOCK:
        log.error("%s on %d different videos within 1 h → treating YouTube as blocked", err.kind, n)
        return YouTubeBlocked("FORBIDDEN_403_REPEATED", f"{n} videos in 1 h; last: {err}")
    log.warning("%s on %s (%d/%d videos in 1 h) — skipping this video", err.kind, video_id, n, STRIKES_TO_BLOCK)
    return err


def video_id_from_url(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{6,})", url or "")
    return m.group(1) if m else str(int(time.time()))


def download_youtube_video(url: str, out_dir: str = None, max_height: int = 1080) -> str:
    out_dir = out_dir or settings.DOWNLOAD_DIR
    os.makedirs(out_dir, exist_ok=True)
    video_path = os.path.join(os.path.abspath(out_dir), f"{video_id_from_url(url)}.mp4")
    if os.path.exists(video_path) and os.path.getsize(video_path) > 1_000_000:
        return video_path
    formats = [f"bestvideo[height<={max_height}][fps<=60]+bestaudio/best[height<={max_height}]/best",
               FORBIDDEN_RETRY_FORMAT]
    for attempt, fmt in enumerate(formats):
        cmd = _base_args() + ["-f", fmt, "--merge-output-format", "mp4", "--no-playlist", "-o", video_path, url]
        log.info("download %s%s", url, " (retry, avc1 format)" if attempt else "")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, encoding="utf-8",
                               errors="replace")
        except subprocess.TimeoutExpired:
            raise DownloadError(f"download timed out: {url}")
        if r.returncode == 0:
            _wait_stable(video_path)
            return video_path
        if os.path.exists(video_path):
            try:
                os.remove(video_path)
            except OSError:
                pass
        err = classify_ytdlp_error(r.stderr)
        if not isinstance(err, VideoForbidden):
            raise err
        if attempt == 0:
            wait = random.uniform(*FORBIDDEN_RETRY_WAIT_S)
            log.warning("%s on %s — waiting %.0f s, then one retry with another format", err.kind, url, wait)
            _sleep(wait)
            continue
        raise _forbidden_failure(video_id_from_url(url), err)


def download_audio_sample(url: str, start: int = 60, end: int = 240, out_dir: str = None) -> str:
    """A few minutes of audio only (source scoring: speech ratio + language)."""
    out_dir = out_dir or os.path.join(settings.WORK_DIR, "samples")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{video_id_from_url(url)}_{start}_{end}.m4a")
    if os.path.exists(out):
        return out
    cmd = _base_args() + ["-f", "bestaudio[ext=m4a]/bestaudio", "--download-sections", f"*{start}-{end}",
                          "--no-playlist", "-o", out, url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not os.path.exists(out):
        err = classify_ytdlp_error(r.stderr)
        raise _forbidden_failure(video_id_from_url(url), err) if isinstance(err, VideoForbidden) else err
    return out


def _wait_stable(path: str, timeout: float = 60):
    """Windows may hold file locks right after the mux step; wait for a stable size."""
    deadline = time.time() + timeout
    prev, stable = -1, 0
    while time.time() < deadline:
        try:
            size = os.path.getsize(path)
        except (FileNotFoundError, PermissionError):
            time.sleep(1)
            continue
        stable = stable + 1 if size == prev and size > 1_000_000 else 0
        prev = size
        if stable >= 2:
            return
        time.sleep(1)
    if not os.path.exists(path) or os.path.getsize(path) < 1_000_000:
        raise DownloadError(f"downloaded file missing or < 1 MB: {path}")
