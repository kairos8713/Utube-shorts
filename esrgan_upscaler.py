"""
esrgan_upscaler.py -- Real-ESRGAN 4x upscale + lanczos downscale wrapper
=======================================================================
Runs AFTER render (1080x1920), BEFORE RIFE interpolation.
Target output: 2160x3840 (4x upscale via realesrgan-x4plus, then
lanczos downscale to target resolution).

NOTE: ESRGAN does 4x the work of a native 2x model.  Expect longer
Stage 2 runtime; elapsed time is printed for production-scale planning.

Pipeline (realesrgan-ncnn-vulkan only supports image input):
  1. FFmpeg extracts all frames as PNG to a temp directory
  2. realesrgan-ncnn-vulkan upscales the entire frame directory (4x)
  3. FFmpeg reassembles upscaled frames with scale=2160:3840:flags=lanczos
     + original audio -> output MP4
  4. Temp directories are cleaned up
"""
import os, subprocess, time, shutil, tempfile, json

ENABLE_ESRGAN_UPSCALE = True
ESRGAN_BINARY = "realesrgan-ncnn-vulkan"  # Must be in PATH

def _probe_fps(video_path: str) -> float:
    """Get video FPS via ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "v:0", video_path],
            capture_output=True, text=True, timeout=15,
        )
        streams = json.loads(r.stdout).get("streams", [{}])
        fps_str = streams[0].get("r_frame_rate", "24/1")
        num, den = map(int, fps_str.split("/"))
        return num / den if den else 24.0
    except Exception:
        return 24.0

def apply_esrgan_upscale(input_mp4: str, output_mp4: str,
                          scale: int = 4,
                          model: str = "realesrgan-x4plus") -> bool:
    """
    Upscale video via Real-ESRGAN using frame-extract pipeline.

    1080x1920 -> 4320x7680 (4x via realesrgan-x4plus) -> 2160x3840 (lanczos)

    Steps:
      1. ffmpeg -i input.mp4 -> frames_in/*.png
      2. realesrgan-ncnn-vulkan -i frames_in -o frames_out -n model -s 4
      3. ffmpeg -i frames_out/%08d.png -vf scale=2160:3840:flags=lanczos
         -i input.mp4 (audio) -> output.mp4
    """
    if not ENABLE_ESRGAN_UPSCALE:
        print("[ESRGAN] Skipped (disabled)")
        return False

    # Verify binary exists
    try:
        subprocess.run([ESRGAN_BINARY, "-h"], capture_output=True, timeout=5)
    except FileNotFoundError:
        print(f"[ESRGAN ERROR] {ESRGAN_BINARY} not found in PATH")
        print("[ESRGAN ERROR] Download from: https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan/releases")
        return False

    t0 = time.time()
    fps = _probe_fps(input_mp4)
    print(f"[ESRGAN] Upscaling {scale}x ({model}): {os.path.basename(input_mp4)} @ {fps}fps")

    tmp_dir = tempfile.mkdtemp(prefix="esrgan_")
    frames_in  = os.path.join(tmp_dir, "frames_in")
    frames_out = os.path.join(tmp_dir, "frames_out")
    os.makedirs(frames_in)
    os.makedirs(frames_out)

    try:
        # -- Step 1: Extract frames -------------------------------------------
        print(f"[ESRGAN] Step 1/3: Extracting frames to {frames_in}")
        r = subprocess.run([
            "ffmpeg", "-y", "-i", input_mp4,
            os.path.join(frames_in, "%08d.png"),
        ], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            print(f"[ESRGAN ERROR] Frame extraction failed: {r.stderr[-500:]}")
            return False

        frame_count = len([f for f in os.listdir(frames_in) if f.endswith(".png")])
        print(f"[ESRGAN] Extracted {frame_count} frames")

        # -- Step 2: Upscale all frames ----------------------------------------
        print(f"[ESRGAN] Step 2/3: Upscaling {frame_count} frames ({scale}x)...")
        r = subprocess.run([
            ESRGAN_BINARY,
            "-i", frames_in,
            "-o", frames_out,
            "-n", model,
            "-s", str(scale),
            "-f", "png",
        ], capture_output=True, text=True, timeout=3600)  # 1 hour timeout
        if r.returncode != 0:
            print(f"[ESRGAN ERROR] Upscale failed: {r.stderr[-500:]}")
            return False

        upscaled_count = len([f for f in os.listdir(frames_out) if f.endswith(".png")])
        print(f"[ESRGAN] Upscaled {upscaled_count}/{frame_count} frames")

        # -- Step 3: Reassemble video + original audio -------------------------
        print(f"[ESRGAN] Step 3/3: Reassembling video @ {fps}fps")
        reassemble_cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", os.path.join(frames_out, "%08d.png"),
            "-i", input_mp4,              # source for audio stream
            "-vf", "scale=2160:3840:flags=lanczos",  # 4x -> target 2160x3840
            "-map", "0:v:0",              # video from upscaled frames
            "-map", "1:a:0?",             # audio from original (? = optional)
            "-c:v", "libx264",            # re-encode (intermediate quality)
            "-preset", "fast",
            "-crf", "18",                 # high quality intermediate
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",               # passthrough audio
            output_mp4,
        ]
        r = subprocess.run(reassemble_cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            print(f"[ESRGAN ERROR] Reassembly failed: {r.stderr[-500:]}")
            return False

        elapsed = time.time() - t0
        print(f"[ESRGAN] OK Upscale complete in {elapsed:.1f}s ({elapsed/60:.1f}min)")
        return os.path.exists(output_mp4)

    except subprocess.TimeoutExpired:
        print("[ESRGAN ERROR] Process timed out")
        return False
    except Exception as e:
        print(f"[ESRGAN ERROR] {e}")
        return False
    finally:
        # Cleanup temp frames (can be many GB)
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
