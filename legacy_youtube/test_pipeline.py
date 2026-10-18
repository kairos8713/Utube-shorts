import os, sys, time, subprocess, json

# Use a small, known-good segment (~45 MB, landscape source, good duration)
SEG_PATH = r"c:/Users/emre8/Desktop/utube/segments/qUUfucHC7tU_part1.mp4"
SPEC = {"start": 5.0, "end": 25.0, "score": 8}

# Fake AI metadata (representative of a real render)
META = dict(
    index          = 999,
    infobox_1      = "Market cap exceeded $2 TRILLION",
    infobox_2      = "Q4 earnings beat by 34%",
    channel_name   = "TechAnalysis",
    theme_color    = "#1E90FF",
    tts_path       = None,
    hook_text      = "This changes EVERYTHING",
    technical_insight = "GPU demand outpacing supply by 3x",
    judgment       = "The data confirms a historic inflection point.",
    cta_question   = "Will AI stocks keep climbing? Drop your take below.",
    sector_name    = "default",
    original_handle= "testchannel",
    font_style     = "Impact",
    accent_color_1 = "#1E90FF",
    accent_color_2 = "#FF6D00",
    accent_color_3 = "#39FF14",
    sfx_set        = "default",
)

OUT_MOVIEPY = r"c:\Users\emre8\Desktop\utube\shorts\test_moviepy.mp4"
OUT_FFMPEG  = r"c:\Users\emre8\Desktop\utube\shorts\test_ffmpeg.mp4"

RUN_MOVIEPY_BASELINE = False  # Toggle to run the 16-minute baseline


def ffprobe_summary(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", path],
            capture_output=True, text=True, timeout=30,
        )
        d = json.loads(r.stdout)
        fmt = d.get("format", {})
        vs  = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
        as_ = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), {})
        return {
            "duration_s":  round(float(fmt.get("duration", 0)), 3),
            "size_mb":     round(int(fmt.get("size", 0)) / 1048576, 2),
            "video_codec": vs.get("codec_name"),
            "resolution":  f"{vs.get('width')}x{vs.get('height')}",
            "fps":         vs.get("avg_frame_rate"),
            "pix_fmt":     vs.get("pix_fmt"),
            "audio_codec": as_.get("codec_name"),
        }
    except Exception as e:
        return {"error": str(e)}


print("=" * 60)
print(" PIPELINE A/B TEST")
print("=" * 60)
print(f"Segment:  {SEG_PATH}")
print(f"Spec:     start={SPEC['start']}s  end={SPEC['end']}s")
print()

# ── Change to project root ────────────────────────────────────────
os.chdir(r"c:\Users\emre8\Desktop\utube")
sys.path.insert(0, r"c:\Users\emre8\Desktop\utube")

import legacy_runner

# ── TEST 1: MoviePy pipeline ──────────────────────────────────────
if RUN_MOVIEPY_BASELINE:
    print("► TEST 1: MoviePy pipeline (USE_FFMPEG_PIPELINE=False)")
    legacy_runner.USE_FFMPEG_PIPELINE = False
    t0 = time.time()
    try:
        raw, elapsed_mp = legacy_runner.generate_vertical_short(SEG_PATH, SPEC, **META)
        import shutil
        shutil.copy(raw, OUT_MOVIEPY)
        print(f"  ✓ MoviePy done in {elapsed_mp:.1f}s  →  {OUT_MOVIEPY}")
    except Exception as e:
        print(f"  ✗ MoviePy FAILED: {e}")
        elapsed_mp = time.time() - t0
else:
    print("► TEST 1: MoviePy pipeline — SKIPPED")
    elapsed_mp = 0.0

# ── TEST 2: FFmpeg pipeline ───────────────────────────────────────
print()
print("► TEST 2: FFmpeg/NVENC pipeline (USE_FFMPEG_PIPELINE=True)")
legacy_runner.USE_FFMPEG_PIPELINE = True
t0 = time.time()
try:
    raw, elapsed_ffm = legacy_runner.generate_vertical_short(SEG_PATH, SPEC, **META)
    import shutil
    shutil.copy(raw, OUT_FFMPEG)
    print(f"  ✓ FFmpeg done in {elapsed_ffm:.1f}s  →  {OUT_FFMPEG}")
except Exception as e:
    print(f"  ✗ FFmpeg FAILED: {e}")
    import traceback; traceback.print_exc()
    elapsed_ffm = time.time() - t0

# ── Results ───────────────────────────────────────────────────────
print()
print("=" * 60)
print(" RESULTS")
print("=" * 60)

if os.path.exists(OUT_MOVIEPY) and os.path.exists(OUT_FFMPEG):
    speedup = elapsed_mp / max(elapsed_ffm, 1)
    print(f"\n{'='*60}")
    print(f"  SPEEDUP:  {speedup:.1f}x faster with FFmpeg")
    print(f"{'='*60}")
