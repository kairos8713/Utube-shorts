"""
ffmpeg_renderer.py — Instagram Reels render pipeline (FFmpeg + NVENC).

Target delivery format
    1080×1920, H.264 ~8 Mbps, 60 fps (30 fps render + RIFE ×2), AAC 48 kHz,
    -14 LUFS, +faststart.

What is on screen (nothing else):
    1. blurred + darkened background, source video full-width in the middle
    2. cinematic polish (grade + light grain)
    3. kinetic word-by-word ASS captions (safe area ≈ y 1290–1420)
    4. small source credit "@handle" just above the video (x=60, y=590)

Audio: original audio + background music → loudnorm -14 LUFS → 48 kHz.

Stages
    render_base()   → 30 fps intermediate with mixed/normalised audio
    (RIFE ×2)       → 60 fps silent video   (rife_interpolator.py)
    finalize_reel() → burn captions + credit, mux audio, final encode
    render_reel()   → convenience wrapper running all three
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Callable, List, Optional

from log_setup import get_logger

log = get_logger("render")

# ── Output config ────────────────────────────────────────────────────────────
OUTPUT_W = 1080
OUTPUT_H = 1920
OUTPUT_FPS = 30                 # render fps; RIFE ×2 → FINAL_FPS
FINAL_FPS = 60
BG_MUSIC_GLOBAL_VOL = 0.07
USE_CINEMATIC_POLISH = True

# Credit text placement (just above a full-width 16:9 video, which starts at y≈656)
CREDIT_X, CREDIT_Y, CREDIT_SIZE, CREDIT_COLOR = 60, 590, 26, "white@0.75"
CREDIT_FONT = "C:/Windows/Fonts/arial.ttf"

# ASS caption styles — MarginV 540 / MarginL,R 140 keep captions out of the
# Reels bottom caption area and the right-hand action buttons.
ASS_MARGIN_V = 540
ASS_MARGIN_LR = 140

LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"
AUDIO_RATE = 48000

_FINAL_NVENC = [
    "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr",
    "-b:v", "8M", "-maxrate", "10M", "-bufsize", "16M",
    "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", "120",
]
_FINAL_X264 = [
    "-c:v", "libx264", "-preset", "medium", "-b:v", "8M", "-maxrate", "10M",
    "-bufsize", "16M", "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", "120",
]
_INTERMEDIATE = [
    "-c:v", "libx264", "-preset", "fast", "-crf", "16", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE),
]


def _nvenc_available() -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                           capture_output=True, text=True, timeout=10)
        return "h264_nvenc" in r.stdout
    except Exception:
        return False


_USE_NVENC = _nvenc_available()


def final_video_flags() -> list:
    return list(_FINAL_NVENC if _USE_NVENC else _FINAL_X264)


# ═════════════════════════════════════════════════════════════════════════════
# helpers
# ═════════════════════════════════════════════════════════════════════════════

def _run_ffmpeg(args: list, label: str, timeout: int = 600) -> bool:
    cmd = ["ffmpeg", "-y", "-hide_banner"] + [str(a) for a in args]
    log.info("[%s] %s", label, " ".join(cmd)[:600])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"[{label}] ffmpeg timed out after {timeout}s")
    if r.returncode != 0:
        log.error("[%s] exit %s\n%s", label, r.returncode, r.stderr[-4000:])
        return False
    return True


def probe_duration(path: str) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
                           capture_output=True, text=True, timeout=30)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def _filter_path(path: str) -> str:
    """Escape a Windows path for use inside a filtergraph option."""
    p = os.path.abspath(path).replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        p = p[0] + "\\:" + p[2:]
    return p.replace("'", "")


def sanitize_handle(handle: Optional[str]) -> str:
    """Keep only characters that are valid in a handle (no filter escaping needed)."""
    h = (handle or "").strip().lstrip("@")
    h = re.sub(r"[^A-Za-z0-9._-]", "", h)
    return h[:40]


# ═════════════════════════════════════════════════════════════════════════════
# ASS kinetic captions
# ═════════════════════════════════════════════════════════════════════════════

POWER_WORDS = {
    "NEVER", "ALWAYS", "STOP", "NOW", "MUST", "ONLY", "BEST", "WORST", "WRONG", "RIGHT",
    "RICH", "POOR", "FAIL", "WIN", "LOSE", "MILLION", "BILLION", "FREE", "TRUTH", "LIE",
    "SECRET", "PROVEN", "GUARANTEED",
}


def _ass_time(s: float) -> str:
    s = max(0.0, s)
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    cs = int(round((sec - int(sec)) * 100))
    if cs == 100:
        cs = 99
    return f"{h}:{m:02d}:{int(sec):02d}.{cs:02d}"


def _emphasis_style(word: str) -> str:
    w = word.strip(".,!?;:\"'").upper()
    if (word.isupper() and len(word) > 2) or any(c.isdigit() for c in word) \
            or "%" in word or "$" in word or w in POWER_WORDS:
        return "Highlight"
    return "Default"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_kinetic_ass(word_segments: List[dict]) -> str:
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {OUTPUT_W}\nPlayResY: {OUTPUT_H}\n"
        "ScaledBorderAndShadow: yes\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Arial,62,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,4,2,2,"
        f"{ASS_MARGIN_LR},{ASS_MARGIN_LR},{ASS_MARGIN_V},1\n"
        f"Style: Highlight,Arial,70,&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,5,3,2,"
        f"{ASS_MARGIN_LR},{ASS_MARGIN_LR},{ASS_MARGIN_V},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for w in word_segments or []:
        word = _ass_escape((w.get("text") or w.get("word") or "").strip())
        if not word:
            continue
        start = float(w.get("start", 0.0))
        end = float(w.get("end", start + 0.3))
        if end - start < 0.20:
            end = start + 0.20
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},{_emphasis_style(word)},,0,0,0,,"
                     f"{{\\fad(80,100)}}{word}")
    return header + "\n".join(lines) + "\n"


def write_ass(word_segments: List[dict], out_path: str) -> Optional[str]:
    if not word_segments:
        return None
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(build_kinetic_ass(word_segments))
    return out_path


# ═════════════════════════════════════════════════════════════════════════════
# filter graphs
# ═════════════════════════════════════════════════════════════════════════════

CINEMATIC_POLISH = (
    "hqdn3d=luma_spatial=4:chroma_spatial=3:luma_tmp=6:chroma_tmp=4.5,"
    "eq=contrast=1.08:saturation=1.15:brightness=-0.02,"
    "colorbalance=rs=-0.05:bs=0.12:rh=0.08:bh=-0.05,"
    "unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount=0.6"
    ":chroma_msize_x=5:chroma_msize_y=5:chroma_amount=0.3,"
    "noise=alls=4:allf=t"
)


def has_audio_stream(path: str) -> bool:
    try:
        r = subprocess.run(["ffprobe", "-v", "quiet", "-select_streams", "a", "-show_entries",
                            "stream=index", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=30)
        return bool(r.stdout.strip())
    except Exception:
        return False


def build_base_filter(start: float, end: float, clip_dur: float, has_bgm: bool,
                      has_audio: bool = True) -> str:
    W, H, fps = OUTPUT_W, OUTPUT_H, OUTPUT_FPS
    parts = [
        f"[0:v]trim=start={start:.4f}:end={end:.4f},setpts=PTS-STARTPTS,split=2[raw_bg][raw_fg]",
        # blurred, darkened background
        f"[raw_bg]scale=540:960:force_original_aspect_ratio=increase:flags=fast_bilinear,"
        f"crop=540:960,gblur=sigma=12,scale={W}:{H}:flags=lanczos,setsar=1,"
        f"colorchannelmixer=rr=0.45:gg=0.45:bb=0.45,fps={fps}[bg]",
        # source video full width, centred
        f"[raw_fg]scale={W}:-2:flags=lanczos,setsar=1,fps={fps}[fg]",
        f"[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2:format=auto:eof_action=pass"
        + (f",{CINEMATIC_POLISH}" if USE_CINEMATIC_POLISH else "") + ",format=yuv420p[vout]",
        # audio: original (padded to exact length) [+ bg music] → loudnorm → 48 kHz
        f"anullsrc=r={AUDIO_RATE}:cl=stereo,atrim=end={clip_dur:.3f},asetpts=PTS-STARTPTS[sil]",
    ]
    if has_audio:
        parts += [
            f"[0:a]atrim=start={start:.4f}:end={end:.4f},asetpts=PTS-STARTPTS,"
            f"aresample={AUDIO_RATE},afade=t=out:st={max(clip_dur - 1.0, 0):.3f}:d=1.0[orig_raw]",
            "[sil][orig_raw]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[orig]",
        ]
    else:
        parts.append("[sil]anull[orig]")
    if has_bgm:
        parts.append(
            f"[1:a]aloop=loop=-1:size=2147483647,atrim=end={clip_dur:.4f},asetpts=PTS-STARTPTS,"
            f"aresample={AUDIO_RATE},volume={BG_MUSIC_GLOBAL_VOL}[bgm]"
        )
        parts.append("[orig][bgm]amix=inputs=2:duration=first:normalize=0[mix]")
    else:
        parts.append("[orig]anull[mix]")
    parts.append(f"[mix]{LOUDNORM},aresample={AUDIO_RATE}[aout]")
    return ";".join(parts)


def build_final_filter(ass_path: Optional[str], credit_handle: str) -> str:
    chain = []
    if ass_path:
        chain.append(f"subtitles=filename='{_filter_path(ass_path)}'")
    if credit_handle:
        chain.append(
            f"drawtext=text='@{credit_handle}':fontfile='{_filter_path(CREDIT_FONT)}'"
            f":fontsize={CREDIT_SIZE}:fontcolor={CREDIT_COLOR}:x={CREDIT_X}:y={CREDIT_Y}"
            ":shadowx=1:shadowy=1:shadowcolor=black@0.6"
        )
    chain.append(f"fps={FINAL_FPS}")
    chain.append("format=yuv420p")
    return "[0:v]" + ",".join(chain) + "[vout]"


# ═════════════════════════════════════════════════════════════════════════════
# stages
# ═════════════════════════════════════════════════════════════════════════════

def render_base(video_path: str, start: float, end: float, out_path: str,
                bgm_path: Optional[str] = None) -> dict:
    """Stage 1: 1080×1920 @ 30 fps intermediate with final (normalised) audio."""
    t0 = time.time()
    file_dur = probe_duration(video_path)
    safe_end = min(end, file_dur - 0.05) if file_dur > 0 else end
    clip_dur = round(safe_end - start, 3)
    if clip_dur <= 1:
        raise RuntimeError(f"Clip too short ({clip_dur:.2f}s) for {video_path}")
    has_bgm = bool(bgm_path and os.path.exists(bgm_path))

    tmp_dir = tempfile.mkdtemp(prefix="ffr_")
    try:
        filt = os.path.join(tmp_dir, "base_filter.txt")
        with open(filt, "w", encoding="utf-8") as f:
            f.write(build_base_filter(start, safe_end, clip_dur, has_bgm, has_audio_stream(video_path)))
        args = ["-loglevel", "warning", "-i", video_path]
        if has_bgm:
            args += ["-i", bgm_path]
        args += ["-/filter_complex", filt, "-map", "[vout]", "-map", "[aout]",
                 "-r", OUTPUT_FPS, "-t", clip_dur] + _INTERMEDIATE + [out_path]
        if not _run_ffmpeg(args, "BASE_RENDER"):
            raise RuntimeError("Base render failed (see render.log)")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return {"path": out_path, "duration": clip_dur, "elapsed": time.time() - t0, "has_bgm": has_bgm}


def finalize_reel(video_path: str, audio_source: str, ass_path: Optional[str],
                  credit_handle: Optional[str], duration: float, out_path: str) -> bool:
    """Stage 3: burn captions + credit on the 60 fps video, mux audio, final encode."""
    handle = sanitize_handle(credit_handle)
    tmp_dir = tempfile.mkdtemp(prefix="ffr_fin_")
    try:
        filt = os.path.join(tmp_dir, "final_filter.txt")
        with open(filt, "w", encoding="utf-8") as f:
            f.write(build_final_filter(ass_path if ass_path and os.path.exists(ass_path) else None, handle))
        args = ["-loglevel", "warning", "-i", video_path, "-i", audio_source,
                "-/filter_complex", filt, "-map", "[vout]", "-map", "1:a:0",
                "-r", FINAL_FPS, "-t", duration] + final_video_flags() + [
                "-c:a", "aac", "-b:a", "192k", "-ar", AUDIO_RATE,
                "-movflags", "+faststart", out_path]
        return _run_ffmpeg(args, "FINAL_ENCODE")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def duplicate_to_60fps(video_path: str, out_path: str) -> bool:
    """RIFE fallback: plain frame duplication to 60 fps (flagged in QC report)."""
    return _run_ffmpeg(["-loglevel", "warning", "-i", video_path, "-an", "-vf", f"fps={FINAL_FPS}",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "16", "-pix_fmt", "yuv420p",
                        out_path], "FPS_DUP")


def render_reel(video_path: str, start: float, end: float, word_segments: List[dict],
                credit_handle: Optional[str], out_path: str, bgm_path: Optional[str] = None,
                interpolate_fn: Optional[Callable[[str, str], bool]] = None,
                work_dir: Optional[str] = None) -> dict:
    """
    Full pipeline. `interpolate_fn(in_30fps, out_60fps) -> bool` is RIFE; when it
    fails (or is None) we fall back to frame duplication and report it.
    Raises on render failure — there is no MoviePy fallback.
    """
    t0 = time.time()
    if work_dir:
        os.makedirs(work_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="reel_", dir=work_dir)
    try:
        base = render_base(video_path, start, end, os.path.join(work, "base30.mp4"), bgm_path)
        ass_path = write_ass(word_segments, os.path.join(work, "captions.ass"))
        v60 = os.path.join(work, "v60.mp4")
        rife_used = bool(interpolate_fn and interpolate_fn(base["path"], v60) and os.path.exists(v60))
        if not rife_used:
            log.warning("RIFE unavailable/failed — falling back to frame duplication")
            if not duplicate_to_60fps(base["path"], v60):
                raise RuntimeError("60 fps conversion failed")
        if not finalize_reel(v60, base["path"], ass_path, credit_handle, base["duration"], out_path):
            raise RuntimeError("Final encode failed (see render.log)")
        return {
            "path": out_path, "duration": base["duration"], "elapsed": time.time() - t0,
            "rife_used": rife_used, "has_captions": bool(ass_path), "has_bgm": base["has_bgm"],
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)
