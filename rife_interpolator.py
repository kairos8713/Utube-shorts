import os
import subprocess
import time

ENABLE_RIFE_INTERPOLATION = True  # render is 24fps via NVENC, interpolate to 60fps (--multi 2 on 30fps base)

def apply_rife_interpolation(input_mp4: str, output_mp4: str) -> bool:
    """
    Executes Practical-RIFE inference to interpolate a 30fps video to 60fps.
    Requires the 'Practical-RIFE' repository to be cloned in the project root.
    """
    if not ENABLE_RIFE_INTERPOLATION:
        print("[RIFE] Skipped (ENABLE_RIFE_INTERPOLATION=False, pipeline already outputs 60fps)")
        return False

    rife_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Practical-RIFE")
    inference_script = os.path.join(rife_dir, "inference_video.py")
    
    if not os.path.exists(inference_script):
        print(f"[RIFE ERROR] Practical-RIFE not found at {rife_dir}")
        return False

    print(f"\n[RIFE] Interpolating to 60fps (multi=2): {os.path.basename(input_mp4)}")
    t0 = time.time()
    
    cmd = [
        "python", "inference_video.py",
        "--video", input_mp4,
        "--output", output_mp4,
        "--multi", "2",
        "--scale", "1.0",
        "--ext", "mp4"
    ]
    
    try:
        # Run inference
        subprocess.run(cmd, cwd=rife_dir, check=True)
        elapsed = time.time() - t0
        print(f"[RIFE] ✓ Interpolation complete in {elapsed:.1f}s: {os.path.basename(output_mp4)}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[RIFE ERROR] Inference failed: {e}")
        return False
