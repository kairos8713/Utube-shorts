"""
rife_interpolator.py — Practical-RIFE frame interpolation, 30 fps → 60 fps.

Uses `--multi 2 --fps 60`. Passing --fps explicitly makes inference_video.py
skip its own (moviepy-based) audio transfer; audio is muxed later by
ffmpeg_renderer.finalize_reel() from the 30 fps base render.

Output is OpenCV mp4v, video-only, 1080×1920 — always re-encoded afterwards.
The caller must hold the GPU lock (gpu_lock.gpu_job).
"""
import os
import subprocess
import sys
import time

from log_setup import get_logger

log = get_logger("rife")

ENABLE_RIFE_INTERPOLATION = True
RIFE_MULTI = 2           # 30 fps × 2 = 60 fps
RIFE_TARGET_FPS = 60
RIFE_TIMEOUT_S = 1800
RIFE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Practical-RIFE")


def rife_available() -> bool:
    return (ENABLE_RIFE_INTERPOLATION
            and os.path.exists(os.path.join(RIFE_DIR, "inference_video.py"))
            and os.path.exists(os.path.join(RIFE_DIR, "train_log", "flownet.pkl")))


def apply_rife_interpolation(input_mp4: str, output_mp4: str) -> bool:
    if not rife_available():
        log.warning("RIFE not available (disabled or model missing at %s)", RIFE_DIR)
        return False
    t0 = time.time()
    cmd = [
        sys.executable, "inference_video.py",
        "--video", os.path.abspath(input_mp4),
        "--output", os.path.abspath(output_mp4),
        "--multi", str(RIFE_MULTI),
        "--fps", str(RIFE_TARGET_FPS),
        "--scale", "1.0",
        "--ext", "mp4",
    ]
    log.info("RIFE ×%d → %dfps: %s", RIFE_MULTI, RIFE_TARGET_FPS, os.path.basename(input_mp4))
    try:
        r = subprocess.run(cmd, cwd=RIFE_DIR, capture_output=True, text=True, timeout=RIFE_TIMEOUT_S,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        log.error("RIFE timed out after %ss", RIFE_TIMEOUT_S)
        return False
    if r.returncode != 0 or not os.path.exists(output_mp4) or os.path.getsize(output_mp4) < 1000:
        log.error("RIFE failed (exit %s): %s", r.returncode, (r.stderr or "")[-2000:])
        return False
    log.info("RIFE done in %.1fs", time.time() - t0)
    return True
