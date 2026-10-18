"""
dedup.py — "the same clip never goes to more than one account".

Two independent signals:
  1. source URL + time-range overlap (any overlap ±2 s with an existing clip)
  2. perceptual hash: 4 frames of the SOURCE clip (20/40/60/80 %) → 4 pHashes.
     A clip is a duplicate only if ALL four frames are within PHASH_MAX_DISTANCE
     bits of another clip's four frames. Single-frame hashes produce false
     positives on podcasts (same studio shot in every episode).
"""
import io
import subprocess
from typing import Optional

from log_setup import get_logger

log = get_logger("dedup")

PHASH_FRAMES = (0.2, 0.4, 0.6, 0.8)
PHASH_MAX_DISTANCE = 6
OVERLAP_TOLERANCE_S = 2.0


def _frame_png(video_path: str, t: float) -> bytes:
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", video_path,
                        "-frames:v", "1", "-vf", "scale=320:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
                       capture_output=True, timeout=60)
    return r.stdout


def clip_phash(video_path: str, start: float, end: float) -> Optional[str]:
    """'h1,h2,h3,h4' hex pHashes of the source clip, or None if frames can't be read."""
    import imagehash
    from PIL import Image
    hashes = []
    for frac in PHASH_FRAMES:
        data = _frame_png(video_path, start + (end - start) * frac)
        if not data:
            return None
        hashes.append(str(imagehash.phash(Image.open(io.BytesIO(data)))))
    return ",".join(hashes)


def hamming_hex(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def phash_match(a: Optional[str], b: Optional[str], max_distance: int = PHASH_MAX_DISTANCE) -> bool:
    if not a or not b:
        return False
    pa, pb = a.split(","), b.split(",")
    if len(pa) != len(pb):
        return False
    return all(hamming_hex(x, y) <= max_distance for x, y in zip(pa, pb))


def clip_conflict(session, source_url: str, start: float, end: float, phash: Optional[str] = None,
                  exclude_video_id: int = None) -> Optional[str]:
    """Reason string if this clip duplicates an existing (non-failed) clip, else None."""
    from models import videos
    q = session.query(videos).filter((videos.qc_status == None) | (videos.qc_status != "FAIL"))  # noqa: E711
    if exclude_video_id:
        q = q.filter(videos.id != exclude_video_id)
    if source_url:
        for v in q.filter(videos.source_url == source_url).all():
            if v.segment_start is None or v.segment_end is None:
                continue
            if start < v.segment_end + OVERLAP_TOLERANCE_S and v.segment_start < end + OVERLAP_TOLERANCE_S:
                return f"segment {start:.0f}-{end:.0f}s overlaps video #{v.id} ({v.segment_start:.0f}-{v.segment_end:.0f}s)"
    if phash:
        for v in q.filter(videos.phash != None).order_by(videos.id.desc()).limit(5000).all():  # noqa: E711
            if phash_match(phash, v.phash):
                return f"perceptual duplicate of video #{v.id}"
    return None
