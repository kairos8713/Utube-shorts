"""Step C: one real production cycle via cycle_runner.run_one_cycle() on the copy DB.

Harness controls (no product code changed):
  * sources_manager.cycle_maintenance → skipped (discovery was step A)
  * downloader.update_ytdlp_once → skipped (the venv was updated today)
  * sources_manager.plan_production → 1 video (10–45 min) per niche with a TRIAL source, within the
    remaining download budget (task rule: max 8 videos for the whole test)
  * every stage timed per clip; QC failures captured; VRAM sampled every 60 s
"""
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

RUN = common.activate()
common.set_step("C")

import cycle_runner  # noqa: E402
import downloader  # noqa: E402
import ffmpeg_renderer  # noqa: E402
import models  # noqa: E402
import production  # noqa: E402
import qwen_specialized as q  # noqa: E402
import render_qc  # noqa: E402
import sources_manager as sm  # noqa: E402
from credit import handle_from_url  # noqa: E402
from models import Decision, ProcessedSourceVideos, Sectors, Source, UploadQueue, videos  # noqa: E402
from timeutil import utcnow  # noqa: E402

TIMES = {"download": [], "split": [], "render": [], "qc": [], "post_render": [], "failures": []}
BUDGET = common.Budget()
PLAN_LOG = []


def harness_plan(session, needs, now=None, yt=None, rng=None):
    yt = sm.YouTubeAPI()
    remaining = BUDGET.limit - len(BUDGET.used())
    cands = []
    for sid in needs:
        trial = (session.query(Source).filter(Source.sector_id == sid, Source.status.in_(("TRIAL", "ACTIVE")))
                 .order_by(Source.score.desc()).first())
        if trial:
            cands.append((trial.score or 0, sid, trial))
    cands.sort(key=lambda x: -x[0])
    plan = []
    for score, sid, src in cands:
        sector = session.get(Sectors, sid)
        if len(plan) >= remaining:
            PLAN_LOG.append({"niche": sector.name, "skipped": "download budget exhausted", "source": src.title})
            continue
        meta = yt.channel_videos(src.channel_url, 30)
        sm.backfill_handle(src, meta)
        used = sm._used_urls(session)
        entries = [e for e in (meta.get("entries") or []) if e and e.get("id")
                   and f"https://www.youtube.com/watch?v={e['id']}" not in used]
        pick = next((e for e in entries if 600 <= (e.get("duration") or 0) <= 2700), None) or \
            next((e for e in entries if (e.get("duration") or 0) >= 480), None)
        if not pick:
            PLAN_LOG.append({"niche": sector.name, "skipped": "no suitable video", "source": src.title})
            continue
        url = f"https://www.youtube.com/watch?v={pick['id']}"
        plan.append(production.SourceVideo(url=url, niche=sector.name, source_id=src.id,
                                           source_handle=src.handle or handle_from_url(src.channel_url),
                                           source_name=src.title or "", music_slug=sector.music_slug, max_clips=3))
        session.add(ProcessedSourceVideos(video_url=url, sector_id=sid, status="PLANNED", processed_at=utcnow()))
        PLAN_LOG.append({"niche": sector.name, "source": src.title, "handle": src.handle, "video": url,
                         "title": pick.get("title"), "duration_s": pick.get("duration")})
    session.commit()
    return plan


def timed(bucket, fn):
    def wrapper(*a, **k):
        t = time.time()
        try:
            return fn(*a, **k)
        finally:
            TIMES[bucket].append({"seconds": round(time.time() - t, 1), "arg": str(a[0])[-60:] if a else ""})
    return wrapper


def budgeted_download(url):
    if not BUDGET.take(f"C source video: {url}"):
        raise downloader.DownloadError("test download budget exhausted")
    return downloader.download_youtube_video(url)


def factory(**kw):
    failed = kw.get("on_clip_failed")

    def on_failed(cand, reason):
        TIMES["failures"].append({"source": cand.source.url, "start": cand.abs_start, "reason": reason})
        if failed:
            failed(cand, reason)
    kw.update(download_fn=timed("download", budgeted_download),
              split_fn=timed("split", production.split_video_into_segments),
              render_fn=timed("render", ffmpeg_renderer.render_reel),
              qc_fn=timed("qc", render_qc.check_video),
              post_render=timed("post_render", kw["post_render"]),
              on_clip_failed=on_failed, delay_fn=lambda: downloader.polite_delay(15, 45))
    return production.Pipeline(**kw)


def sampler(stop):
    while not stop.is_set():
        common.sample_resources("C running")
        stop.wait(60)


def ffprobe_summary(path):
    qc = render_qc.check_video(path)
    m = qc.metrics
    return {k: m.get(k) for k in ("width", "height", "fps", "vcodec", "bitrate", "acodec", "sample_rate",
                                  "integrated_lufs", "true_peak_dbfs", "faststart", "duration", "size_bytes")} | \
        {"qc_passed": qc.passed, "qc_reasons": qc.reasons, "qc_warnings": qc.warnings}


def frames(path, tag):
    dur = render_qc.check_video(path).metrics.get("duration") or 20
    out = []
    for name, t in (("0.5s", 0.5), ("middle", dur / 2), ("end", max(dur - 0.3, 0))):
        png = os.path.join(RUN, "frames", f"{tag}_{name}.png")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1", png],
                       capture_output=True, timeout=60)
        out.append(png)
    return out


def boundary_check(text):
    words = (text or "").split()
    if not words:
        return {"first": None, "last": None}
    return {"first_words": " ".join(words[:5]), "last_words": " ".join(words[-5:]),
            "starts_capitalised": words[0][:1].isupper(), "ends_with_punctuation": words[-1][-1:] in ".!?"}


def main():
    sm.cycle_maintenance = lambda session, now=None, llm=None, yt=None: {"skipped": "discovery ran in step A"}
    sm.plan_production = harness_plan
    downloader.update_ytdlp_once = lambda: None
    client = common.instrumented_llm()
    s = models.Session()
    stop = threading.Event()
    threading.Thread(target=sampler, args=(stop,), daemon=True).start()
    t0 = time.time()
    try:
        stats = cycle_runner.run_one_cycle(s, llm=client, pipeline_factory=factory)
    finally:
        stop.set()
    total = round(time.time() - t0, 1)

    reels = []
    vids = s.query(videos).filter(videos.qc_status == "PASS").order_by(videos.id).all()
    for i, v in enumerate(vids):
        src = s.get(Source, v.source_id) if v.source_id else None
        dec = s.query(Decision).filter(Decision.task == "clip_score",
                                       Decision.proposed_action.like(f'%"video_id": {v.id},%')).first()
        caption_lines = (v.caption or "").splitlines()
        reels.append({
            "video_id": v.id, "account_id": v.owned_channel_id, "niche": s.get(Sectors, v.sector_id).name,
            "source": src.title if src else None, "source_handle": src.handle if src else None,
            "source_url": v.source_url, "segment": [v.segment_start, v.segment_end], "qwen_score": v.qwen_point,
            "topic": v.topic, "qwen_reason": dec.reason if dec else None, "caption": v.caption,
            "caption_chars": len(v.caption or ""), "hashtags": len((v.hashtags or "").split()),
            "credit_line": caption_lines[-1] if caption_lines else None,
            "credit_ok": bool(src and src.handle and caption_lines and caption_lines[-1] == f"🎥 @{src.handle}"),
            "transcript_boundaries": boundary_check(v.transcript), "cover_offset_ms": v.cover_offset_ms,
            "ffprobe": ffprobe_summary(v.file_path) if v.file_path and os.path.exists(v.file_path) else None,
            "frames": frames(v.file_path, f"reel{v.id}") if i < 3 and v.file_path and os.path.exists(v.file_path) else [],
            "qc_report": json.loads(v.qc_report or "{}"),
        })
    comments = [r["caption"].split("\n\n")[0] for r in reels if r["caption"]]
    sims = sorted(((round(q.caption_similarity(a, b), 2), i, j) for i, a in enumerate(comments)
                   for j, b in enumerate(comments) if i < j), reverse=True)[:5]
    result = {"total_seconds": total, "cycle_stats": stats, "plan": PLAN_LOG, "times": TIMES,
              "reels": reels, "reel_count": len(reels), "caption_similarity_top5": sims,
              "queue": [{"id": r.id, "account": r.owned_channel_id, "video": r.video_id, "at": str(r.scheduled_for),
                         "slot_source": r.slot_source} for r in s.query(UploadQueue).order_by(UploadQueue.scheduled_for)],
              "qwen": common.qwen_stats("C"), "budget_used": BUDGET.used(),
              "resources": common.sample_resources("C end")}
    common.save_result("C", result)
    print(json.dumps({"total_seconds": total, "reels": len(reels), "failures": len(TIMES["failures"]),
                      "stage_seconds": stats.get("stage_seconds"), "qwen": result["qwen"]}, indent=1))


if __name__ == "__main__":
    main()
