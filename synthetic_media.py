"""
synthetic_media.py — generate test footage with ffmpeg lavfi (no downloads).
Used by tests and the end-to-end DRY_RUN when yt-dlp is unavailable.
"""
import subprocess


def make_source_video(out_path: str, duration: float = 30.0, size: str = "1920x1080",
                      fps: int = 30, tone_hz: int = 440, black_start: float = 0.0) -> str:
    """Colourful moving test pattern + a tone that varies (so it is not 'silent')."""
    vf = "null"
    if black_start > 0:
        vf = f"drawbox=t=fill:c=black:enable='lt(t,{black_start})'"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency={tone_hz}:sample_rate=48000:duration={duration}:beep_factor=4",
        "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest", out_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    return out_path


def make_tone(out_path: str, duration: float = 30.0, hz: int = 220) -> str:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"sine=frequency={hz}:sample_rate=48000:duration={duration}", out_path],
                   check=True, capture_output=True, timeout=120)
    return out_path
