"""
media_utils.py — small media helpers shared by the production pipeline
(segment split, timecodes, background music lookup, GPU memory flush).
"""
import gc
import json
import math
import os
import random
import re
import subprocess
from typing import List, Optional

from config import ROOT_DIR, settings
from log_setup import get_logger

log = get_logger("media")

CHUNK_DURATION_SEC = 180
BG_MUSIC_DIR = os.path.join(ROOT_DIR, "bg_music")
AUDIO_EXT = (".mp3", ".wav", ".ogg", ".m4a")


def seconds_to_timecode(seconds) -> str:
    return f"{int(seconds // 60):02}:{int(seconds % 60):02}"


def parse_timecode(tc: str) -> float:
    parts = tc.strip().replace(",", ".").split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        return float(parts[0])
    except ValueError:
        return 0.0


def intervals_overlap(a_start: float, a_end: float, b_start: float, b_end: float,
                      tolerance: float = 0.0) -> bool:
    return a_start < b_end + tolerance and b_start < a_end + tolerance


def truncate_words(text: str, max_words: int = 1500) -> str:
    words = text.split()
    return text if len(words) <= max_words else " ".join(words[:max_words])


def cleanup_gpu():
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception:
        pass
    gc.collect()


MIN_FFMPEG_MAJOR = 7          # `-/filter_complex <file>` (filter script option) needs FFmpeg 7.0+
MIN_LIBAVFILTER_MAJOR = 10    # FFmpeg 7.0 ships libavfilter 10.x (git builds print no version number)


def ffmpeg_version_ok(version_text: str = None):
    """(ok, detail). Uses 'ffmpeg version N.M' when present, else the libavfilter major."""
    if version_text is None:
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-version"], capture_output=True, text=True, timeout=20)
            version_text = r.stdout or ""
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"ffmpeg not runnable: {e}"
    m = re.search(r"ffmpeg version n?(\d+)\.(\d+)", version_text)
    if m:
        major = int(m.group(1))
        return major >= MIN_FFMPEG_MAJOR, f"ffmpeg {m.group(1)}.{m.group(2)} (need >= {MIN_FFMPEG_MAJOR}.0)"
    m = re.search(r"libavfilter\s+(\d+)\.\s*(\d+)", version_text)
    if m:
        major = int(m.group(1))
        return major >= MIN_LIBAVFILTER_MAJOR, \
            f"libavfilter {m.group(1)}.{m.group(2)} (need >= {MIN_LIBAVFILTER_MAJOR}, i.e. FFmpeg >= {MIN_FFMPEG_MAJOR})"
    return False, "could not determine the FFmpeg version"


def probe(path: str) -> dict:
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format",
                        "-show_streams", path], capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout or "{}")


def duration_of(path: str) -> float:
    try:
        return float(probe(path)["format"]["duration"])
    except Exception:
        return 0.0


def extract_audio(video_path: str, start: float, end: float, out_path: str) -> bool:
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                        "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", out_path],
                       capture_output=True, text=True, timeout=300)
    return r.returncode == 0 and os.path.exists(out_path)


def split_video_into_segments(video_path: str, out_dir: str = None,
                              chunk: int = CHUNK_DURATION_SEC) -> List[dict]:
    """Stream-copy split. Returns [{'path', 'offset'}] — offset = start in source (s)."""
    out_dir = out_dir or settings.SEGMENTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    duration = duration_of(video_path)
    if duration <= 0:
        raise RuntimeError(f"cannot probe duration: {video_path}")
    base = os.path.splitext(os.path.basename(video_path))[0]
    out = []
    for i in range(math.ceil(duration / chunk)):
        start = i * chunk
        path = os.path.join(out_dir, f"{base}_part{i + 1}.mp4")
        if not (os.path.exists(path) and os.path.getsize(path) > 1024):
            r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(start), "-t", str(chunk),
                                "-i", video_path, "-c", "copy", "-avoid_negative_ts", "make_zero",
                                "-movflags", "+faststart", path],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0 or not os.path.exists(path):
                log.warning("segment %d failed: %s", i + 1, r.stderr[-300:])
                continue
        out.append({"path": path, "offset": float(start)})
    return out


# ── background music ─────────────────────────────────────────────────────────

def music_slug(name: str) -> str:
    """'Tech / AI (Riskli)' → 'tech_ai_riskli' (must match bg_music folder names)."""
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def _tracks(folder: str) -> list:
    if not os.path.isdir(folder):
        return []
    return [os.path.join(folder, f) for f in sorted(os.listdir(folder)) if f.lower().endswith(AUDIO_EXT)]


def pick_bg_music(sector_name: str = None, slug: str = None, rng=random) -> Optional[str]:
    """sector folder → bg_music/default → None. `slug` (sectors.music_slug) wins over the name."""
    for key in [slug, music_slug(sector_name or ""), "default"]:
        if not key:
            continue
        tracks = _tracks(os.path.join(BG_MUSIC_DIR, key))
        if tracks:
            return rng.choice(tracks)
    log.info("no background music for sector=%s slug=%s", sector_name, slug)
    return None


def available_music_slugs() -> list:
    if not os.path.isdir(BG_MUSIC_DIR):
        return []
    return sorted(d for d in os.listdir(BG_MUSIC_DIR) if _tracks(os.path.join(BG_MUSIC_DIR, d)))
