"""
daily_production.py — Daily IG Reels Production Automation
===========================================================
Designed to run once per day via Windows Task Scheduler.
No manual trigger required.

For each active OwnedChannel (IG account):
  1. Pick next pool Channel via round-robin (channel_pool_index)
  2. yt-dlp the latest unprocessed video from that channel
  3. Clip-score via Qwen → pick best 20-25s clip
  4. Pipeline: render_raw_ffmpeg → RIFE → burn_overlays_ffmpeg
  5. Generate caption + hashtags via Qwen
  6. Write MP4 + caption TXT to pending/{account_id}/{date}_{clip_id}.mp4

Failure policy: if one account fails at any step, log and continue
to the next — never abort the whole batch.

Log: logs/daily_production_YYYYMMDD.log
"""

import os
import sys
import gc
import re
import uuid
import json
import time
import logging
import datetime
import subprocess
import traceback

# ── Ensure project root on path ───────────────────────────────────────────────
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT_DIR)

# ── DB ────────────────────────────────────────────────────────────────────────
from sqlalchemy import create_engine, text as sqla_text
from sqlalchemy.orm import sessionmaker
from models import (
    Base, Channels, Sectors, OwnedChannels,
    videos, ProcessedSourceVideos, DB_PATH,
)

engine  = create_engine(DB_PATH, echo=False)
Session = sessionmaker(bind=engine)

# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_DIR = os.path.join(ROOT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_today_str = datetime.date.today().strftime("%Y%m%d")
_log_file  = os.path.join(LOG_DIR, f"daily_production_{_today_str}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[
        logging.FileHandler(_log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("daily_production")

# ── Output dirs ───────────────────────────────────────────────────────────────
PENDING_BASE   = os.path.join(ROOT_DIR, "pending")
DOWNLOADS_DIR  = os.path.join(ROOT_DIR, "downloads")
os.makedirs(PENDING_BASE,  exist_ok=True)
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ── Pipeline constants ────────────────────────────────────────────────────────
MIN_VIDEO_SCORE = 7     # minimum Qwen clip score to accept
MAX_DL_RETRY    = 2     # download retry attempts per video
MAX_POOL_SCAN   = 5     # max pool channels to try before giving up on this account


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _is_processed(url, session):
    """Check if a source URL is already in processed_source_videos."""
    r = session.execute(
        sqla_text("SELECT 1 FROM processed_source_videos WHERE video_url = :url LIMIT 1"),
        {"url": url},
    )
    return r.fetchone() is not None


def _mark_processed(url, sector_id, status, source_channel_id, session):
    try:
        rec = ProcessedSourceVideos(
            video_url=url,
            sector_id=sector_id,
            source_channel_id=source_channel_id,
            status=status,
            processed_at=datetime.datetime.utcnow(),
        )
        session.add(rec)
        session.commit()
    except Exception as e:
        log.warning("_mark_processed failed: %s", e)
        session.rollback()


def _fetch_latest_url(channel_url):
    """
    Use yt-dlp flat-playlist to get the single newest non-short video URL
    from a channel. Returns watch URL string or None.
    """
    base_url = channel_url.rstrip("/")
    for suffix in ["/shorts", "/featured", "/videos", "/streams", "/about"]:
        base_url = base_url.split(suffix)[0]
    target = base_url + "/videos"

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--match-filter", "!is_live & !is_upcoming & availability=public",
        "--playlist-end", "10",
        target,
    ]
    try:
        proc = subprocess.run(
            cmd, shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
        )
        if proc.returncode != 0:
            log.warning("yt-dlp error for %s: %s",
                        channel_url,
                        proc.stderr.decode("utf-8", errors="replace")[:200])
            return None
        data    = json.loads(proc.stdout)
        entries = data.get("entries") or []
        for entry in entries:
            if not entry:
                continue
            vid_id   = entry.get("id")
            duration = entry.get("duration")
            if not vid_id:
                continue
            if duration is not None and duration < 60:
                continue   # skip Shorts
            return "https://www.youtube.com/watch?v=" + vid_id
        return None
    except Exception as e:
        log.warning("_fetch_latest_url exception: %s", e)
        return None


def _download_video(url):
    """
    Download a YouTube video to downloads/ dir.
    Returns absolute path on success, None on failure.
    """
    import legacy_runner
    for attempt in range(1, MAX_DL_RETRY + 1):
        try:
            path = legacy_runner.download_youtube_video(url)
            return path
        except Exception as e:
            log.warning("Download attempt %d/%d failed: %s", attempt, MAX_DL_RETRY, e)
            if attempt < MAX_DL_RETRY:
                subprocess.run(
                    [sys.executable, "-m", "yt_dlp", "--rm-cache-dir"],
                    check=False, capture_output=True,
                )
                time.sleep(5)
    return None


def _pick_best_clip(video_path, sector_name):
    """
    Transcribe first segment with Whisper, send to Qwen for clip scoring,
    return best clip spec dict {start, end, score, seg_path, transcript} or None.
    """
    import legacy_runner

    try:
        segments = legacy_runner.split_video_into_segments(video_path)
        if not segments:
            log.warning("No segments from %s", os.path.basename(video_path))
            return None
    except Exception as e:
        log.warning("Segment split failed: %s", e)
        return None

    scoring_system = (
        "You are a viral clip extractor. Scan the transcript and output ONLY clip timestamps.\n"
        "No analysis. No explanation. No prose.\n\n"
        "SCORING (0-9):\n"
        "0-6 = boring/skip. NEVER include these.\n"
        "7   = mildly interesting.\n"
        "8   = high-energy, surprising, or funny.\n"
        "9   = TOP 1% ONLY: shock, scream, massive fail, hysteria.\n\n"
        "OUTPUT FORMAT - one line per clip, nothing else:\n"
        "INDEX/SCORE/START - END\n"
        "Example: 1/8/00:15 - 00:37\n\n"
        "Rules: duration 20-25 s. Top 3 clips. Zero extra text."
    )

    best_clip = None

    for seg_path in segments[:3]:
        legacy_runner.cleanup_gpu()
        transcript = legacy_runner.generate_segment_transcript(seg_path)
        if not transcript:
            continue

        safe_t = legacy_runner._truncate_transcript(transcript)
        messages = [
            {"role": "system", "content": scoring_system},
            {"role": "user",
             "content": "Transcript:\n" + safe_t + "\n\nFind the top 3 viral clips (20-25 s each)."},
        ]

        try:
            reply = legacy_runner.send_prompt_to_qwen(messages, max_tokens=400)
        except Exception as e:
            log.warning("Qwen scoring error: %s", e)
            continue

        for line in reply.splitlines():
            m = re.search(r"(\d+)/(\d+)/(\d+[:.]\d+)\s*-\s*(\d+[:.]\d+)", line)
            if not m:
                continue
            score = int(m.group(2))
            start = legacy_runner.parse_timecode(m.group(3))
            end   = legacy_runner.parse_timecode(m.group(4))
            dur   = end - start
            if score < MIN_VIDEO_SCORE:
                continue
            if not (20 <= dur <= 25):
                continue
            if best_clip is None or score > best_clip["score"]:
                best_clip = {
                    "start": start, "end": end, "score": score,
                    "seg_path": seg_path, "transcript": transcript,
                }
        if best_clip:
            break

    return best_clip


def _generate_ig_caption(clip, sector_name):
    """
    Minimal Qwen call: given a clip's transcript and sector, produce
    an IG caption + hashtag list only. No judgment, no hook, no TTS.

    Returns dict with keys: title, caption, hashtags (list).
    Falls back to safe defaults on any failure.
    """
    import legacy_runner

    _FALLBACK = {
        "title":    sector_name + " Clip",
        "caption":  "",
        "hashtags": [],
    }

    transcript = clip.get("transcript", "")
    if not transcript.strip():
        return _FALLBACK

    safe_t = legacy_runner._truncate_transcript(transcript)

    system_msg = (
        "You are a social-media caption writer for Instagram Reels.\n"
        "Output ONLY a raw JSON object with exactly three keys:\n"
        "  title    : short punchy title (max 8 words)\n"
        "  caption  : 1-2 sentence IG caption, no emojis, no hashtags\n"
        "  hashtags : JSON array of 5-10 relevant hashtag strings (include the # sign)\n"
        "No markdown, no explanation, no extra keys."
    )
    user_msg = (
        f"SECTOR: {sector_name}\n"
        f"TRANSCRIPT EXCERPT:\n{safe_t}\n\n"
        f"Output only the raw JSON object."
    )
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user",   "content": user_msg},
    ]

    import re, json as _json
    try:
        legacy_runner.cleanup_gpu()
        raw = legacy_runner.send_prompt_to_qwen(messages, max_tokens=300)
        m = re.search(r"(\{[\s\S]*\})", raw)
        if not m:
            log.warning("Caption Qwen: no JSON block found")
            return _FALLBACK
        data = _json.loads(m.group(1))
        return {
            "title":    str(data.get("title",    _FALLBACK["title"])),
            "caption":  str(data.get("caption",  "")),
            "hashtags": data.get("hashtags", []) if isinstance(data.get("hashtags"), list) else [],
        }
    except Exception as e:
        log.warning("Caption Qwen failed: %s", e)
        return _FALLBACK


def _run_pipeline(clip, account, clip_index, orig_handle, session):
    """
    Run 3-stage render pipeline for a clip.
    Returns (final_path, ig_meta) on success, or None on failure.
    ig_meta contains only: title, caption, hashtags.
    """
    import legacy_runner
    import rife_interpolator
    from ffmpeg_renderer import render_raw_ffmpeg, burn_overlays_ffmpeg

    seg_path    = clip["seg_path"]
    spec        = {"start": clip["start"], "end": clip["end"], "score": clip["score"]}
    sector_name = account.sector.name if account.sector else "default"
    accent_color_1 = getattr(account, "accent_color_1", "#1E90FF") or "#1E90FF"

    # ── Caption + hashtags via Qwen (lean call — no TTS, no judgment) ─────────
    ig_meta = _generate_ig_caption(clip, sector_name)

    # ── Stage 1: Raw render (1080x1920 @ 24fps) ───────────────────────────────
    legacy_runner.cleanup_gpu()
    raw_path, ass_path, clip_dur, render_elapsed = render_raw_ffmpeg(
        seg_path, spec, clip_index,
        sector_name=sector_name,
        original_handle=orig_handle,
        _transcribe_fn=legacy_runner.transcribe_subclip_for_karaoke,
        _pick_bgm_fn=legacy_runner.pick_bg_music,
    )
    log.info("[acc=%d] Stage 1 done in %.1fs -> %s",
             account.id, render_elapsed, os.path.basename(raw_path))

    # ── Stage 2: RIFE 3x interpolation (-> 72fps) ────────────────────────────
    legacy_runner.cleanup_gpu()
    rife_out_path = raw_path.replace(".mp4", "_72fps.mp4")
    rife_ok = rife_interpolator.apply_rife_interpolation(
        input_mp4=raw_path,
        output_mp4=rife_out_path,
    )
    if not rife_ok:
        log.error("[acc=%d] RIFE failed", account.id)
        return None
    log.info("[acc=%d] Stage 2 (RIFE) done", account.id)

    # ── Stage 3: Overlay + NVENC ──────────────────────────────────────────────
    final_path = raw_path.replace(".mp4", "_final.mp4")
    overlay_ok = burn_overlays_ffmpeg(
        video_path=rife_out_path,
        audio_path=raw_path,
        ass_path=ass_path,
        clip_duration=clip_dur,
        output_path=final_path,
        accent_color_1=accent_color_1,
    )

    for _tmp in [rife_out_path]:
        if os.path.exists(_tmp):
            try: os.remove(_tmp)
            except: pass

    if not overlay_ok or not os.path.exists(final_path):
        log.error("[acc=%d] burn_overlays failed", account.id)
        return None
    log.info("[acc=%d] Stage 3 (burn) done -> %s",
             account.id, os.path.basename(final_path))

    return final_path, ig_meta


def _write_pending(account, final_path, ai_meta):
    """
    Move rendered MP4 and write caption TXT into pending/{account_id}/.
    Returns destination MP4 path.
    """
    import shutil
    pending_dir = os.path.join(PENDING_BASE, str(account.id))
    os.makedirs(pending_dir, exist_ok=True)

    date_str = datetime.date.today().strftime("%Y%m%d")
    clip_id  = uuid.uuid4().hex[:8]
    stem     = date_str + "_" + clip_id
    dest_mp4 = os.path.join(pending_dir, stem + ".mp4")
    dest_txt = os.path.join(pending_dir, stem + ".txt")

    try:
        os.rename(final_path, dest_mp4)
    except OSError:
        shutil.move(final_path, dest_mp4)

    hashtags = ai_meta.get("hashtags", [])
    hashtag_str = " ".join(hashtags) if isinstance(hashtags, list) else str(hashtags or "")
    caption_body = "\n\n".join(filter(None, [
        ai_meta.get("title", ""),
        ai_meta.get("caption", ""),
        hashtag_str,
    ])).strip()

    with open(dest_txt, "w", encoding="utf-8") as f:
        f.write(caption_body)

    log.info("[acc=%d] Pending -> %s", account.id, dest_mp4)
    return dest_mp4


# ══════════════════════════════════════════════════════════════════════════════
#  Per-account production worker
# ══════════════════════════════════════════════════════════════════════════════

def process_account(account, session):
    """
    Full production run for one IG account.
    Returns True on success (clip produced + pending written), False otherwise.
    """
    log.info("=== [acc=%d] %s ===", account.id, account.name or "account_" + str(account.id))

    if not account.sector:
        log.warning("[acc=%d] No sector assigned - skipping", account.id)
        return False

    pool_channels = (
        session.query(Channels)
        .filter(Channels.sector_id == account.sector_id)
        .all()
    )
    if not pool_channels:
        log.warning("[acc=%d] No pool channels for sector %s - skipping",
                    account.id, account.sector.name)
        return False

    n_pool      = len(pool_channels)
    start_index = account.channel_pool_index or 0
    source_url  = None
    used_channel = None

    for attempt in range(min(MAX_POOL_SCAN, n_pool)):
        idx     = (start_index + attempt) % n_pool
        pool_ch = pool_channels[idx]
        log.info("[acc=%d] Trying pool[%d/%d]: %s", account.id, idx, n_pool - 1, pool_ch.url)

        candidate_url = _fetch_latest_url(pool_ch.url)
        if not candidate_url:
            log.info("[acc=%d] No valid URL from %s", account.id, pool_ch.url)
            continue

        if _is_processed(candidate_url, session):
            log.info("[acc=%d] Already processed: %s", account.id, candidate_url)
            continue

        source_url    = candidate_url
        used_channel  = pool_ch
        account.channel_pool_index = (idx + 1) % n_pool
        session.commit()
        break

    if not source_url:
        log.warning("[acc=%d] No fresh video found after %d pool tries",
                    account.id, min(MAX_POOL_SCAN, n_pool))
        return False

    log.info("[acc=%d] Source: %s", account.id, source_url)

    # Download
    video_path = _download_video(source_url)
    if not video_path:
        log.error("[acc=%d] Download failed - skipping", account.id)
        _mark_processed(source_url, account.sector_id,
                        "PERMANENT_DL_FAIL", used_channel.id, session)
        return False
    log.info("[acc=%d] Downloaded: %s", account.id, os.path.basename(video_path))

    # Pick clip
    clip = _pick_best_clip(video_path, account.sector.name)
    if not clip:
        log.warning("[acc=%d] No qualifying clip (min score %d)", account.id, MIN_VIDEO_SCORE)
        _mark_processed(source_url, account.sector_id,
                        "PROCESSED_NO_CLIPS", used_channel.id, session)
        return False
    log.info("[acc=%d] Best clip: score=%d  %.1f-%.1fs",
             account.id, clip["score"], clip["start"], clip["end"])

    orig_handle = used_channel.url.split("/@")[-1].split("/")[0] \
        if "/@" in used_channel.url else ""

    # Pipeline
    t_pipe = time.time()
    try:
        result = _run_pipeline(
            clip=clip,
            account=account,
            clip_index=account.id * 1000 + int(time.time()) % 1000,
            orig_handle=orig_handle,
            session=session,
        )
    except Exception as e:
        log.error("[acc=%d] Pipeline exception: %s\n%s",
                  account.id, e, traceback.format_exc())
        result = None

    if result is None:
        _mark_processed(source_url, account.sector_id,
                        "FAILED_RENDER", used_channel.id, session)
        return False

    final_path, ig_meta = result
    log.info("[acc=%d] Pipeline done in %.1fs", account.id, time.time() - t_pipe)

    # Write pending
    try:
        _write_pending(account, final_path, ig_meta)
    except Exception as e:
        log.error("[acc=%d] Failed to write pending: %s", account.id, e)
        _mark_processed(source_url, account.sector_id,
                        "FAILED_OUTPUT", used_channel.id, session)
        return False

    _mark_processed(source_url, account.sector_id,
                    "PROCESSED_SUCCESS", used_channel.id, session)
    log.info("[acc=%d] SUCCESS", account.id)
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    t0 = time.time()
    log.info("=== daily_production.py START  %s ===",
             datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    session = Session()
    try:
        accounts = (
            session.query(OwnedChannels)
            .filter(OwnedChannels.is_active == True)
            .all()
        )
        log.info("Active accounts: %d", len(accounts))
    except Exception as e:
        log.critical("Cannot load accounts from DB: %s", e)
        sys.exit(1)

    total     = len(accounts)
    succeeded = 0
    failed    = 0

    for account in accounts:
        try:
            ok = process_account(account, session)
            if ok:
                succeeded += 1
            else:
                failed += 1
        except Exception as e:
            log.error("[acc=%d] Unhandled exception: %s\n%s",
                      account.id, e, traceback.format_exc())
            failed += 1
        finally:
            try:
                import legacy_runner
                legacy_runner.cleanup_gpu()
            except Exception:
                pass
            gc.collect()

    elapsed = time.time() - t0
    log.info(
        "\n=== SUMMARY ===\n"
        "  Total accounts:  %d\n"
        "  Succeeded:       %d\n"
        "  Failed/skipped:  %d\n"
        "  Total elapsed:   %.1f s (%.1f min)\n"
        "=== daily_production.py END ===",
        total, succeeded, failed, elapsed, elapsed / 60,
    )


if __name__ == "__main__":
    main()
