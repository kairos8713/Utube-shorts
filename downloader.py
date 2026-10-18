"""
downloader.py — yt-dlp wrappers (download + flat metadata), YouTube health.

* Bot / cookie errors are recognised and raised as YouTubeBlocked so the cycle
  can stop all downloads and raise an alert instead of hammering YouTube.
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

from config import settings
from log_setup import get_logger

log = get_logger("downloader")

BOT_HINTS = (
    "sign in to confirm you", "not a bot", "confirm you're not a bot",
    "http error 429", "too many requests",
)
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


def classify_ytdlp_error(stderr: str):
    s = (stderr or "").lower()
    if any(h in s for h in BOT_HINTS):
        return YouTubeBlocked("BOT_CHECK", stderr)
    if any(h in s for h in COOKIE_HINTS):
        return YouTubeBlocked("COOKIES", stderr)
    if "http error 403" in s and ("po token" in s or "unable to download video data" in s):
        # 2026-10: YouTube demands a GVS PO token → every download fails the same way
        return YouTubeBlocked("FORBIDDEN_403_PO_TOKEN", stderr)
    if any(h in s for h in PRIVATE_HINTS):
        return VideoUnavailable(stderr[:300])
    return DownloadError(stderr[-500:] if stderr else "yt-dlp failed")


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


def update_ytdlp_once():
    global _updated_this_process
    if _updated_this_process:
        return
    _updated_this_process = True
    log.info("Updating yt-dlp (once per cycle)")
    subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
                   check=False, capture_output=True, timeout=300)


def _base_args() -> list:
    args = [sys.executable, "-m", "yt_dlp"]
    if os.path.exists(settings.NODE_PATH):
        args += ["--js-runtimes", f"node:{settings.NODE_PATH}", "--remote-components", "ejs:github"]
    if os.path.exists(settings.COOKIES_PATH):
        args += ["--cookies", settings.COOKIES_PATH]
    return args


def ytdlp_json(target: str, extra: list = None, timeout: int = 120) -> dict:
    """`yt-dlp --dump-single-json` for channels/searches/videos (no download)."""
    cmd = _base_args() + ["--dump-single-json", "--no-warnings"] + (extra or []) + [target]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        raise classify_ytdlp_error(r.stderr)
    return json.loads(r.stdout or "{}")


def video_id_from_url(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{6,})", url or "")
    return m.group(1) if m else str(int(time.time()))


def download_youtube_video(url: str, out_dir: str = None, max_height: int = 1080) -> str:
    out_dir = out_dir or settings.DOWNLOAD_DIR
    os.makedirs(out_dir, exist_ok=True)
    video_path = os.path.join(os.path.abspath(out_dir), f"{video_id_from_url(url)}.mp4")
    if os.path.exists(video_path) and os.path.getsize(video_path) > 1_000_000:
        return video_path
    cmd = _base_args() + [
        "--extractor-args", "youtube:player_client=android_vr,web,tv",
        "-f", f"bestvideo[height<={max_height}][fps<=60]+bestaudio/best[height<={max_height}]/best",
        "--merge-output-format", "mp4", "--no-playlist",
        "-o", video_path, url,
    ]
    log.info("download %s", url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, encoding="utf-8",
                           errors="replace")
    except subprocess.TimeoutExpired:
        raise DownloadError(f"download timed out: {url}")
    if r.returncode != 0:
        if os.path.exists(video_path):
            try:
                os.remove(video_path)
            except OSError:
                pass
        raise classify_ytdlp_error(r.stderr)
    _wait_stable(video_path)
    return video_path


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
        raise classify_ytdlp_error(r.stderr)
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
