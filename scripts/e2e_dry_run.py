"""
e2e_dry_run.py — one-account, one-source mini cycle, fully DRY_RUN.

  python scripts/e2e_dry_run.py [--synthetic] [--keep]

download (yt-dlp; falls back to synthetic testsrc + local SAPI speech) → Whisper
(real) → Qwen (Ollama if healthy, else mock) → render + RIFE (real) → QC → queue
→ simulated publish (DRY_RUN: nothing sent) → simulated comments + replies →
cycle report. The produced reel is verified with ffprobe (1080×1920, 60 fps,
h264, AAC 48 kHz, ~8 Mbps, faststart, ≈ -14 LUFS) and frames at 0.5 s / middle /
end are extracted and checked (no panels, first frame not darkened, captions in
the safe area, credit visible). Everything runs in a temp DB; results go to
logs/e2e/<timestamp>/.
"""
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
STAMP = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
OUT = os.path.join(ROOT, "logs", "e2e", STAMP)
TMP = tempfile.mkdtemp(prefix="e2e_")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.update({
    "REELS_NO_DOTENV": "1", "DRY_RUN": "true", "EMAIL_ENABLED": "true", "EMAIL_TEST_MODE": "true",
    "DATABASE_URL": "sqlite:///" + os.path.join(TMP, "e2e.db").replace("\\", "/"),
    "LOG_DIR": os.path.join(OUT, "logs"), "WORK_DIR": os.path.join(TMP, "work"),
    "BACKUP_DIR": os.path.join(TMP, "backups"), "REELS_DIR": os.path.join(TMP, "reels"),
    "DOWNLOAD_DIR": os.path.join(TMP, "downloads"), "SEGMENTS_DIR": os.path.join(TMP, "segments"),
    "GPU_LOCK_PATH": os.path.join(TMP, "gpu.lock"), "FERNET_KEY": Fernet.generate_key().decode(),
    "IG_UPLOAD_METHOD": "r2_url", "COOKIES_PATH": os.path.join(ROOT, "cookies.txt"),
})
os.makedirs(OUT, exist_ok=True)

SPEECH = ("Most people think success comes from one big decision, but it really comes from a hundred small "
          "ones that nobody sees. When we started, we had three customers and no money. Every morning we asked "
          "one question: what is the smallest thing we can ship today. That habit changed everything. "
          "Within a year we had three million users, and the strange part is that we never ran a single ad. ")


def log(msg):
    print(f"[E2E] {msg}", flush=True)


def sapi_speech(path: str, text: str) -> bool:
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.Rate = 0; $s.SetOutputToWaveFile('{path}'); $s.Speak('{text}'); $s.Dispose()")
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                       "powershell.exe")
    try:
        r = subprocess.run([exe if os.path.exists(exe) else "powershell", "-NoProfile", "-NonInteractive",
                            "-Command", ps], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"SAPI unavailable: {e}")
        return False
    return r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 10000


def synthetic_source(path: str) -> str:
    wav = os.path.join(TMP, "speech.wav")
    if sapi_speech(wav, SPEECH * 3):
        dur = float(json.loads(subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format",
                                               wav], capture_output=True, text=True).stdout)["format"]["duration"])
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"testsrc2=size=1920x1080:rate=30:duration={dur:.2f}", "-i", wav,
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-ar", "48000", "-shortest", path], check=True, capture_output=True, timeout=600)
        log(f"synthetic source with SAPI speech ({dur:.0f}s)")
    else:
        from synthetic_media import make_source_video
        make_source_video(path, duration=120)
        log("synthetic source with tone (SAPI unavailable — captions will be empty)")
    return path


def try_real_download(url_channel: str):
    import downloader
    try:
        meta = downloader.ytdlp_json(url_channel.rstrip("/") + "/videos", ["--flat-playlist", "--playlist-end", "8"],
                                     timeout=120)
        entry = next(e for e in meta.get("entries") or [] if e and (e.get("duration") or 0) >= 480)
        url = f"https://www.youtube.com/watch?v={entry['id']}"
        log(f"downloading real source {url} ({entry.get('duration')}s)")
        return url, downloader.download_youtube_video(url, max_height=1080)
    except Exception as e:
        log(f"real download unavailable ({type(e).__name__}: {str(e)[:160]}) → synthetic")
        return None, None


def verify_reel(path: str, duration: float) -> dict:
    import render_qc
    qc = render_qc.check_video(path, duration)
    m = qc.metrics
    checks = {
        "resolution_1080x1920": (m.get("width"), m.get("height")) == (1080, 1920),
        "fps_60": abs((m.get("fps") or 0) - 60) < 0.6,
        "h264": m.get("vcodec") == "h264",
        "aac_48k": m.get("acodec") == "aac" and m.get("sample_rate") == 48000,
        "bitrate_about_8mbps": 5_000_000 <= (m.get("bitrate") or 0) <= 11_000_000,
        "faststart": m.get("faststart") is True,
        "loudness_about_-14": m.get("integrated_lufs") is not None and abs(m["integrated_lufs"] + 14) <= 1.5,
        "qc_passed": qc.passed,
    }
    return {"checks": checks, "metrics": {k: m.get(k) for k in ("width", "height", "fps", "vcodec", "acodec",
                                                                  "sample_rate", "bitrate", "faststart",
                                                                  "integrated_lufs", "true_peak_dbfs", "duration",
                                                                  "size_bytes")},
            "qc_reasons": qc.reasons, "qc_warnings": qc.warnings}


def frame_checks(path: str, duration: float, has_captions: bool) -> dict:
    import cv2
    import numpy as np
    shots = {"0.5s": 0.5, "middle": duration / 2, "end": max(duration - 0.3, 0)}
    res = {}
    for name, t in shots.items():
        png = os.path.join(OUT, f"frame_{name}.png")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1", png],
                       check=True, capture_output=True)
        img = cv2.imread(png)
        grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)          # uint8 (Laplacian needs it)
        top = grey[0:500, :]          # blurred background only — any panel/box/button would be sharp here
        bottom = grey[1500:1920, :]
        sharp_top = float(cv2.Laplacian(top, cv2.CV_64F).var())
        sharp_bottom = float(cv2.Laplacian(bottom, cv2.CV_64F).var())
        centre = grey[700:1220, :]
        # captions: bright pixels in the safe band y 1290–1420, x 140–940
        band = grey[1280:1430, 140:940]
        bright_band = float((band > 235).mean())
        credit = grey[588:622, 60:420]
        res[name] = {"png": png, "mean_luma_centre": round(float(centre.mean()), 1),
                     "sharpness_top": round(sharp_top, 1), "sharpness_bottom": round(sharp_bottom, 1),
                     "caption_band_bright_ratio": round(bright_band, 4),
                     "credit_region_max": float(credit.max()), "credit_region_mean": round(float(credit.mean()), 1)}
    first = res["0.5s"]
    checks = {
        "first_frame_not_darkened": first["mean_luma_centre"] > 60,
        "no_panels_top_or_bottom": all(r["sharpness_top"] < 60 and r["sharpness_bottom"] < 60 for r in res.values()),
        "credit_visible": all(r["credit_region_max"] > 170 for r in res.values()),
        "captions_in_safe_band": (not has_captions) or any(r["caption_band_bright_ratio"] > 0.002 for r in res.values()),
    }
    return {"frames": res, "checks": checks}


def main():
    synthetic = "--synthetic" in sys.argv
    import db_migrate  # noqa: F401
    import models
    from crypto_utils import encrypt
    from llm_client import OllamaClient, set_llm
    from llm_mock import MockLLM
    from models import Comment, CommentReply, OwnedChannels, Post, Sectors, Source, UploadQueue, videos

    s = models.Session()
    llm = OllamaClient()
    backend = "ollama"
    if not llm.health():
        llm, backend = MockLLM(), "mock"
    set_llm(llm)
    log(f"LLM backend: {backend}")

    now = _dt.datetime.utcnow()
    sec = Sectors(name="Podcast (Riskli)", title="Podcast", description="Podcast", tags="podcast",
                  music_slug="podcast_riskli")
    s.add(sec)
    s.flush()
    acc = OwnedChannels(name="e2e_test", ig_username="e2e_test", sector_id=sec.id, level=1, mail="-",
                        password=encrypt("-"), status="ACTIVE", daily_post_cap=1, ig_user_id="17841400000000000",
                        ig_access_token=encrypt("DRY-RUN-TOKEN"), ig_token_expires_at=now + _dt.timedelta(days=50),
                        ig_token_refreshed_at=now - _dt.timedelta(days=10),
                        ig_setup_completed_at=now - _dt.timedelta(days=3), activated_at=now - _dt.timedelta(days=1))
    src = Source(channel_url="https://www.youtube.com/@MrBeast", handle="MrBeast", title="MrBeast", sector_id=sec.id,
                 status="TRIAL")
    s.add_all([acc, src])
    s.commit()

    # 1. source footage
    url, path = (None, None) if synthetic else try_real_download(src.channel_url)
    real = path is not None
    if not real:
        url = "https://www.youtube.com/watch?v=SYNTHETIC01"
        path = synthetic_source(os.path.join(TMP, "SYNTHETIC01.mp4"))
        src.handle = "E2E_Source"
        s.commit()

    # 2-5. production (real Whisper, LLM, real render + RIFE, QC)
    import media_utils
    from production import Pipeline, SourceVideo
    from production_store import ProductionStore, assign_videos
    store = ProductionStore(s, cycle_id=1)
    kept = {}

    def ready(cand):
        v = store.on_clip_ready(cand)
        if v is not None:
            dst = os.path.join(OUT, "reel.mp4")
            if "reel" not in kept:
                shutil.copy2(cand.out_path, dst)
                kept["reel"] = dst
                kept["duration"] = cand.render_info["duration"]
                kept["info"] = cand.render_info
                kept["caption"] = v.caption
                kept["words"] = len(cand.words)
    pipe = Pipeline(llm=llm, is_duplicate=store.is_duplicate, previous_captions=store.previous_captions,
                    post_render=store.post_render, on_clip_ready=ready, on_clip_failed=store.on_clip_failed,
                    split_fn=lambda p: media_utils.split_video_into_segments(p)[:2], delay_fn=lambda: None)
    t0 = _dt.datetime.utcnow()
    result = pipe.run([SourceVideo(url=url, niche=sec.name, source_id=src.id, source_handle=src.handle,
                                   source_name=src.title, music_slug=sec.music_slug, max_clips=1, local_path=path)])
    log(f"production: produced={len(result.produced)} failed={len(result.failed)} "
        f"errors={[x.error for x in result.source_errors]} stages={ {k: round(v) for k, v in result.stage_seconds.items()} }")
    if not result.produced:
        log("no clip produced — aborting")
        return 1

    # 6. queue + simulated publish
    import scheduler
    from publish_runner import publish_due
    assign_videos(s)
    n = scheduler.schedule_all(s, now)
    item = s.query(UploadQueue).filter_by(status="PENDING").order_by(UploadQueue.scheduled_for).first()
    log(f"scheduled {n}; first slot {item.scheduled_for} UTC ({item.slot_source})")
    publish_at = item.scheduled_for + _dt.timedelta(seconds=5)
    outcomes = publish_due(s, publish_at)
    p = s.query(Post).first()
    log(f"publish outcome {outcomes}; post status={p.status if p else None} media={p.ig_media_id if p else None}")

    # 7. simulated comments + replies
    import comment_responder as cr
    comments = [("q1", "What was the smallest thing they shipped first?"),
                ("r1", "This is my video, you stole it. Take it down."),
                ("s1", "earn money fast at http://spam.example"),
                ("e1", "🔥🔥")]
    for cid, text in comments:
        s.add(Comment(ig_comment_id=cid, post_id=p.id, owned_channel_id=acc.id, username="viewer", text=text,
                      commented_at=publish_at + _dt.timedelta(minutes=20), status="NEW"))
    s.commit()
    llm_replies = cr.process_new(s, publish_at + _dt.timedelta(minutes=25))
    sent = cr.send_due(s, publish_at + _dt.timedelta(minutes=100))
    log(f"comments: {llm_replies}; replies sent (dry): {sent}")

    # 8. report
    import reports
    rep = reports.build_cycle_report(s, 1, t0 - _dt.timedelta(hours=1), _dt.datetime.utcnow() + _dt.timedelta(hours=3))
    log(f"report #{rep.id}: {rep.narrative[:120]}")

    # verification
    verification = verify_reel(kept["reel"], kept["duration"])
    frames = frame_checks(kept["reel"], kept["duration"], kept["words"] > 0)
    from instagram.client import IGClient
    summary = {
        "stamp": STAMP, "llm_backend": backend, "real_download": real, "source_url": url,
        "render": {k: kept["info"].get(k) for k in ("duration", "elapsed", "rife_used", "has_captions", "has_bgm")},
        "caption": kept["caption"], "caption_words_timed": kept["words"],
        "verification": verification, "frames": frames,
        "publish": {"outcomes": outcomes, "post_status": p.status, "dry_run": p.dry_run, "media_id": p.ig_media_id},
        "comments": {c.ig_comment_id: c.status for c in s.query(Comment).all()},
        "replies": [(r.text, r.status) for r in s.query(CommentReply).all()],
        "report_id": rep.id,
        "outbox": sorted(os.listdir(os.path.join(os.environ["LOG_DIR"], "outbox")))
        if os.path.isdir(os.path.join(os.environ["LOG_DIR"], "outbox")) else [],
        "reel_file_deleted_after_publish": not os.path.exists(s.get(videos, p.video_id).file_path or "x"),
    }
    all_checks = {**verification["checks"], **frames["checks"], "published_dry_run": p.status == "PUBLISHED" and p.dry_run}
    summary["all_checks"] = all_checks
    summary["passed"] = all(all_checks.values())
    with open(os.path.join(OUT, "e2e_result.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, default=str)
    log(json.dumps(all_checks, indent=1))
    log(f"RESULT: {'PASS' if summary['passed'] else 'FAIL'} → {OUT}")
    s.close()
    if "--keep" not in sys.argv:
        shutil.rmtree(TMP, ignore_errors=True)
    return 0 if summary["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
