"""
test_render.py — Full 3-stage pipeline isolation test
=====================================================
Uses test_clip.mp4 as source, calls render_raw_ffmpeg() for a fresh
Stage 1 raw render (with setsar=1 fix), then runs RIFE →
burn_overlays_ffmpeg sequentially.

ESRGAN upscale removed — 12.46s/frame at 1080x1920 is non-viable
at production scale (480 frames/clip × 20 accounts on single 3060).

Expected final output: 1080x1920, 72fps, h264, SAR 1:1.

Fail-fast: if ANY stage fails, the script stops and reports the error.
"""
import subprocess, json, os, sys, time

# ── Configuration ─────────────────────────────────────────────────────────────
SRC_VIDEO  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_clip.mp4")
SPEC       = {"start": 5.0, "end": 25.0, "score": 8}
TEST_INDEX = 999

# ── Clean up stale reels artifacts before test ────────────────────────────────
_reels_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reels")
if os.path.isdir(_reels_dir):
    _stale = [f for f in os.listdir(_reels_dir) if f.endswith(".mp4") or f.endswith(".ass")]
    if _stale:
        print(f"[CLEANUP] Removing {len(_stale)} stale artifact(s) from reels/")
        for f in _stale:
            try:
                os.remove(os.path.join(_reels_dir, f))
            except Exception as e:
                print(f"  [WARN] Could not remove {f}: {e}")

# ── Helpers ───────────────────────────────────────────────────────────────────

def ffprobe(path: str) -> dict:
    """Run ffprobe and return parsed JSON with all stream info."""
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", path],
        capture_output=True, text=True, timeout=30,
    )
    return json.loads(r.stdout)


def print_ffprobe_report(path: str, stage_name: str):
    """Print a detailed ffprobe report for a stage output."""
    info = ffprobe(path)
    size_mb = round(int(info.get("format", {}).get("size", 0)) / 1048576, 2)
    duration = info.get("format", {}).get("duration", "?")
    print(f"\n  📁 File: {os.path.basename(path)} ({size_mb} MB, {duration}s)")

    has_audio = False
    for s in info.get("streams", []):
        if s["codec_type"] == "video":
            print(f"  🎥 Video: {s['width']}x{s['height']}  "
                  f"codec={s.get('codec_name')}  "
                  f"fps={s.get('r_frame_rate')}  "
                  f"SAR={s.get('sample_aspect_ratio', 'N/A')}  "
                  f"DAR={s.get('display_aspect_ratio', 'N/A')}  "
                  f"pix_fmt={s.get('pix_fmt')}")
        elif s["codec_type"] == "audio":
            has_audio = True
            print(f"  🔊 Audio: codec={s.get('codec_name')}  "
                  f"sample_rate={s.get('sample_rate')}  "
                  f"channels={s.get('channels')}")
    if not has_audio:
        print("  🔇 Audio: NONE (video-only)")


def fail(msg: str):
    """Print error and exit."""
    print(f"\n❌ FATAL: {msg}")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 1: Raw render (1080×1920 @ 24fps)
# ══════════════════════════════════════════════════════════════════════════════
print("=" * 70)
print(" STAGE 1: Raw FFmpeg Render (1080x1920 @ 24fps)")
print("=" * 70)

if not os.path.exists(SRC_VIDEO):
    fail(f"Source video not found: {SRC_VIDEO}")

from ffmpeg_renderer import render_raw_ffmpeg

t0 = time.time()
try:
    raw_path, ass_path, clip_dur, render_elapsed = render_raw_ffmpeg(
        SRC_VIDEO, SPEC, TEST_INDEX,
        sector_name="default",
        original_handle="testchannel",
        _transcribe_fn=None,    # skip Whisper for isolation test
        _pick_bgm_fn=None,      # skip bg music for isolation test
    )
except Exception as e:
    fail(f"render_raw_ffmpeg raised: {e}")

if not raw_path or not os.path.exists(raw_path):
    fail(f"Raw render produced no output file (raw_path={raw_path})")

print(f"\n✅ Stage 1 PASSED ({render_elapsed:.1f}s)")
print_ffprobe_report(raw_path, "Stage 1 (raw)")


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 2: RIFE 3x Interpolation (→ 72fps, video-only)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print(" STAGE 2: RIFE 3x Interpolation (1080x1920 → 72fps)")
print("=" * 70)

import rife_interpolator

rife_out_path = raw_path.replace(".mp4", "_72fps.mp4")
t1 = time.time()
rife_ok = rife_interpolator.apply_rife_interpolation(
    input_mp4=raw_path,
    output_mp4=rife_out_path,
)
elapsed_rife = time.time() - t1

if not rife_ok:
    fail("RIFE interpolation returned False — check [RIFE ERROR] messages above")

if not os.path.exists(rife_out_path):
    fail(f"RIFE output file missing: {rife_out_path}")

print(f"\n✅ Stage 2 PASSED ({elapsed_rife:.1f}s)")
print_ffprobe_report(rife_out_path, "Stage 2 (RIFE)")


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 3: Overlay Burn-in + Final NVENC Encode
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print(" STAGE 3: Overlay Burn-in + Final NVENC Encode (1080x1920 @ 72fps)")
print("=" * 70)

from ffmpeg_renderer import burn_overlays_ffmpeg

final_path = raw_path.replace(".mp4", "_FINAL.mp4")
t2 = time.time()
overlay_ok = burn_overlays_ffmpeg(
    video_path=rife_out_path,
    audio_path=raw_path,        # audio from Stage 1 raw render
    ass_path=ass_path,          # may be None if Whisper was skipped
    clip_duration=clip_dur,
    output_path=final_path,
    accent_color_1="#1E90FF",
)
elapsed_overlay = time.time() - t2

if not overlay_ok:
    fail("burn_overlays_ffmpeg returned False — check [OVERLAY] messages above")

if not os.path.exists(final_path):
    fail(f"Final output file missing: {final_path}")

print(f"\n✅ Stage 3 PASSED ({elapsed_overlay:.1f}s)")
print_ffprobe_report(final_path, "Stage 3 (final)")


# ══════════════════════════════════════════════════════════════════════════════
#  SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
total = time.time() - t0
print("\n" + "=" * 70)
print(" ALL 3 STAGES PASSED ✅")
print("=" * 70)
print(f"  Stage 1 (raw render):   {render_elapsed:.1f}s  → {os.path.basename(raw_path)}")
print(f"  Stage 2 (RIFE):         {elapsed_rife:.1f}s  → {os.path.basename(rife_out_path)}")
print(f"  Stage 3 (final):        {elapsed_overlay:.1f}s  → {os.path.basename(final_path)}")
print(f"  TOTAL:                  {total:.1f}s ({total/60:.1f} min)")
print("=" * 70)