"""
cover_frame.py — pick the Reels cover frame from the rendered clip.

Candidates: 8 frames between 10 % and 90 % of the clip. Score =
  sharpness (variance of Laplacian, normalised) + face bonus (Haar cascade)
  + contrast (grey std-dev).
Result is used as the container's `thumb_offset` (ms), so no image hosting is
needed; a JPEG copy is saved for the panel.

Profile-grid safety: the grid shows a 3:4 centre crop (y 240–1680 of 1920).
Burned-in text sits at y≈590 (credit) and y≈1290–1420 (captions) → inside it.
GRID_SAFE_Y is asserted in tests against ffmpeg_renderer's layout constants.
"""
import os
import subprocess
from typing import Optional

from log_setup import get_logger

log = get_logger("cover")

GRID_SAFE_Y = (240, 1680)
N_CANDIDATES = 8


def _grab(video: str, t: float, out_jpg: str) -> bool:
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1",
                        "-q:v", "3", out_jpg], capture_output=True, timeout=60)
    return r.returncode == 0 and os.path.exists(out_jpg)


def score_image(path: str) -> dict:
    import cv2
    img = cv2.imread(path)
    if img is None:
        return {"score": -1}
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # only the source-video band (avoid the blurred background dominating)
    h = grey.shape[0]
    band = grey[int(h * 0.30):int(h * 0.70), :]
    sharp = float(cv2.Laplacian(band, cv2.CV_64F).var())
    contrast = float(band.std())
    faces = 0
    try:
        cascade = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
        faces = len(cascade.detectMultiScale(band, scaleFactor=1.2, minNeighbors=5, minSize=(60, 60)))
    except Exception:
        pass
    score = min(sharp / 500.0, 1.0) * 0.5 + min(contrast / 64.0, 1.0) * 0.3 + (0.2 if faces else 0.0)
    return {"score": round(score, 4), "sharpness": round(sharp, 1), "contrast": round(contrast, 1), "faces": faces}


def select_cover(video: str, duration: float, out_dir: str) -> Optional[dict]:
    os.makedirs(out_dir, exist_ok=True)
    best = None
    base = os.path.splitext(os.path.basename(video))[0]
    for i in range(N_CANDIDATES):
        t = duration * (0.10 + 0.80 * i / max(N_CANDIDATES - 1, 1))
        jpg = os.path.join(out_dir, f"{base}_cand{i}.jpg")
        if not _grab(video, t, jpg):
            continue
        s = score_image(jpg)
        s.update(offset_ms=int(t * 1000), path=jpg)
        if best is None or s["score"] > best["score"]:
            if best:
                _rm(best["path"])
            best = s
        else:
            _rm(jpg)
    if best:
        final = os.path.join(out_dir, f"{base}_cover.jpg")
        os.replace(best["path"], final)
        best["path"] = final
        log.info("cover %s @%dms score=%.3f", os.path.basename(video), best["offset_ms"], best["score"])
    return best


def _rm(p):
    try:
        os.remove(p)
    except OSError:
        pass
