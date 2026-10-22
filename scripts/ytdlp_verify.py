"""
ytdlp_verify.py — end-to-end check of YouTube access through the PRODUCTION code paths
(downloader.download_youtube_video, sources_manager.YouTubeAPI) with the real .env settings.

  .venv\\Scripts\\python.exe scripts\\ytdlp_verify.py

1. ytsearch (YouTubeAPI.search) for long podcasts → pick one 30–70 min episode
2. channel listing (YouTubeAPI.channel_videos) → newest 3 videos of a channel
3. 5 downloads (the podcast + 4 fixed public videos), random 15–45 s pause between them,
   each verified with ffprobe (video + audio stream, duration), then deleted.
Results: logs/ytdlp_fix/verify_<utc>.json
"""
import datetime as _dt
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import downloader  # noqa: E402
from sources_manager import YouTubeAPI  # noqa: E402

SEARCH = "podcast full episode interview"
CHANNEL = "https://www.youtube.com/@BlenderOfficial"   # from oEmbed author_url
FIXED = [
    "https://www.youtube.com/watch?v=aqz-KE-bpKQ",   # Big Buck Bunny (Blender), ~10 min
    "https://www.youtube.com/watch?v=eRsGyueVLvQ",   # Sintel (Blender), ~15 min
    "https://www.youtube.com/watch?v=YQHsXMglC9A",   # Adele - Hello, ~6 min
    "https://www.youtube.com/watch?v=kJQP7kiw5Fk",   # Despacito, ~5 min
]
MAX_DOWNLOADS = 5


def ffprobe(path):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
                       capture_output=True, text=True, timeout=60)
    info = json.loads(r.stdout or "{}")
    kinds = sorted({s.get("codec_type") for s in info.get("streams", [])})
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"duration_s": round(float(info.get("format", {}).get("duration") or 0), 1), "streams": kinds,
            "height": v.get("height"), "vcodec": v.get("codec_name")}


def main():
    out = {"started": _dt.datetime.utcnow().isoformat(), "python": downloader.ytdlp_python(),
           "base_args": downloader._base_args()[1:], "steps": {}}
    ver = subprocess.run([downloader.ytdlp_python(), "-m", "yt_dlp", "--version"], capture_output=True, text=True)
    out["yt_dlp"] = ver.stdout.strip()
    yt = YouTubeAPI()

    # 1. search
    t = time.time()
    hits = yt.search(SEARCH, 25)
    long_ones = [e for e in hits if e and 1800 <= (e.get("duration") or 0) <= 4200]
    out["steps"]["ytsearch"] = {"ok": bool(hits), "results": len(hits), "30_70_min": len(long_ones),
                                "seconds": round(time.time() - t, 1)}
    print(f"[search] {len(hits)} results, {len(long_ones)} between 30 and 70 min")
    podcast = f"https://www.youtube.com/watch?v={long_ones[0]['id']}" if long_ones else None

    # 2. channel listing (newest 3)
    time.sleep(random.uniform(5, 15))
    t = time.time()
    try:
        meta = yt.channel_videos(CHANNEL, 3)
        latest = [{"id": e.get("id"), "title": e.get("title"), "duration": e.get("duration")}
                  for e in (meta.get("entries") or [])][:3]
        err = None
    except downloader.DownloadError as e:
        latest, err = [], str(e)[-1000:]
    out["steps"]["channel_latest_3"] = {"ok": len(latest) == 3, "channel": CHANNEL, "videos": latest,
                                        "error": err, "seconds": round(time.time() - t, 1)}
    print(f"[channel] {CHANNEL}: {[v['title'] for v in latest]}")

    # 3. downloads
    targets = ([podcast] if podcast else []) + FIXED
    targets = targets[:MAX_DOWNLOADS]
    work = tempfile.mkdtemp(prefix="ytverify_")
    downloads = []
    try:
        for i, url in enumerate(targets):
            if i:
                downloader.polite_delay(15, 45)
            t = time.time()
            rec = {"url": url, "podcast": url == podcast}
            try:
                path = downloader.download_youtube_video(url, out_dir=work)
                rec.update(ok=True, mb=round(os.path.getsize(path) / 1e6, 1), **ffprobe(path))
                rec["ok"] = rec["ok"] and "video" in rec["streams"] and "audio" in rec["streams"]
                os.remove(path)
            except downloader.DownloadError as e:
                rec.update(ok=False, error=type(e).__name__, detail=str(e)[-2000:])
            rec["seconds"] = round(time.time() - t, 1)
            downloads.append(rec)
            print(f"[download] {'OK ' if rec['ok'] else 'FAIL'} {url} {rec.get('mb', '')} MB "
                  f"{rec.get('duration_s', '')} s {rec.get('height', '')}p {rec['seconds']} s")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    out["steps"]["downloads"] = downloads
    out["passed"] = (out["steps"]["ytsearch"]["ok"] and out["steps"]["channel_latest_3"]["ok"]
                     and len(downloads) == 5 and all(d["ok"] for d in downloads)
                     and any(d["podcast"] and d.get("duration_s", 0) >= 1800 for d in downloads))
    os.makedirs(os.path.join(ROOT, "logs", "ytdlp_fix"), exist_ok=True)
    path = os.path.join(ROOT, "logs", "ytdlp_fix", f"verify_{_dt.datetime.utcnow():%Y%m%dT%H%M%SZ}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"RESULT: {'PASS' if out['passed'] else 'FAIL'} -> {path}")   # ASCII: cp1252 console
    return 0 if out["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
