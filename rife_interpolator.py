import os
import subprocess
import time

ENABLE_RIFE_INTERPOLATION = True  # render is 24fps, interpolate to 72fps (--multi 3)

def apply_rife_interpolation(input_mp4: str, output_mp4: str) -> bool:
    """
    Executes Practical-RIFE inference to interpolate a 24fps video to 72fps.
    Uses --multi 3 (RIFE v4.25 supports arbitrary multi via direct interpolation).
    Input is 1080x1920 (native resolution, no ESRGAN upscale).

    Output is MPEG-4 Part 2 (mp4v via OpenCV), video-only (no audio).
    A final re-encode via burn_overlays_ffmpeg() is mandatory.
    """
    if not ENABLE_RIFE_INTERPOLATION:
        print("[RIFE] Skipped (ENABLE_RIFE_INTERPOLATION=False)")
        return False

    rife_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Practical-RIFE")
    inference_script = os.path.join(rife_dir, "inference_video.py")
    
    if not os.path.exists(inference_script):
        print(f"[RIFE ERROR] Practical-RIFE not found at {rife_dir}")
        return False

    multi = 3       # 24fps x 3 = 72fps (RIFE v4.25 direct interpolation)
    target_fps = 72
    print(f"\n[RIFE] Interpolating to {target_fps}fps (multi={multi}): {os.path.basename(input_mp4)}")
    t0 = time.time()
    
    cmd = [
        "python", "inference_video.py",
        "--video", input_mp4,
        "--output", output_mp4,
        "--multi", str(multi),
        "--scale", "1.0",
        "--ext", "mp4",
    ]
    
    try:
        subprocess.run(cmd, cwd=rife_dir, check=True)
        elapsed = time.time() - t0
        print(f"[RIFE] OK Interpolation complete in {elapsed:.1f}s: {os.path.basename(output_mp4)}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[RIFE ERROR] Inference failed: {e}")
        return False
