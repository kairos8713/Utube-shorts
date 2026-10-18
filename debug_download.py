import yt_dlp
import os
import shutil

def check_ffmpeg():
    if shutil.which("ffmpeg"):
        print("[INFO] ffmpeg is installed and found in PATH.")
    else:
        print("[ERROR] ffmpeg NOT found in PATH. yt-dlp requires ffmpeg to merge video/audio.")

def test_download():
    url = "https://www.youtube.com/watch?v=QJI0an6irrA"
    print(f"[DEBUG] Attempting to download: {url}")
    
    opts = {
        'verbose': True,
        'format': 'best', 
        'outtmpl': 'debug_download_%(title)s.%(ext)s',
        'force_generic_extractor': False,
        # Try spoofing Android client to avoid web-based throttling/SABR issues
        'extractor_args': {'youtube': {'player_client': ['android']}}
    }
    
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        print("[SUCCESS] Download completed.")
    except Exception as e:
        print(f"[FAIL] Download Exception: {e}")

if __name__ == "__main__":
    check_ffmpeg()
    test_download()
