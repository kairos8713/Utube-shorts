"""
UTube Bot - Haftalık Döngüsel Video Üretim & Zamanlanmış Yayın Sistemi
====================================================================
DÖNGÜ:
  Faz 0: İstatistikler & Seviye Güncelle
  Faz 1: Haftalık ihtiyaç hesapla (level × 7 gün)
  Faz 2: Pool kanallarından sektör bazlı video topla  → GAP-FIRST LOGIC
         Her OwnedChannel için:
           1. Gap hesapla (7 günlük pencerede eksik video sayısı)
           2. Pool kanallarını sırayla tara (ilki önce, gap dolunca dur)
         → Qwen clip scoring + structured JSON metadata
         → Kokoro TTS voiceover (cuda:0 — Single-GPU AI Brain)
         → Render + info-box + subscribe button + TTS mix (cuda:0 — NVENC)
  Faz 3: Upload kuyruğu oluştur (7 güne eşit aralıklarla dağıt)
  Faz 4: YouTube'a zamanlanmış yükle
  Faz 5: Shorts klasörünü temizle → yeni döngü

SINGLE-GPU MODE (1x RTX 3060 12 GB — cuda:0 for everything):
  cuda:0 → Whisper + Qwen2.5-14B-AWQ + Kokoro TTS + FFmpeg / MoviePy / h264_nvenc

VRAM Discipline (One-Model-at-a-Time Protocol):
  Between every segment:
    1. del model  (Whisper & Qwen freed explicitly in finally blocks)
    2. gc.collect() + torch.cuda.empty_cache() on cuda:0
    3. Model loaded only after VRAM flush is confirmed clean
  Between every render:
    4. gc.collect() + torch.cuda.empty_cache() BEFORE and AFTER rendering
    5. time.sleep(10) → VRAM buffer clearance before next model load
====================================================================
"""
# CUDA DLL fix for AutoAWQ kernels (MUST be before any AutoAWQ/torch import)
import os
if hasattr(os, "add_dll_directory"):
    _cuda_bin = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.1\bin"
    if os.path.isdir(_cuda_bin):
        os.add_dll_directory(_cuda_bin)
import sys
import time
import datetime
import subprocess
import json
import re
import shutil
import traceback
import logging

logger = logging.getLogger(__name__)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import (
    Base, Channels, Sectors, OwnedChannels,
    videos, logs, ProcessedSourceVideos, UploadQueue, DB_PATH
)
import legacy_runner
from publisher import YouTubePublisher
from email_notifier import send_alert

# ─── DB Setup ───────────────────────────────────────────────────────────────
engine  = create_engine(DB_PATH)
Session = sessionmaker(bind=engine)
session = Session()

# ─── Konfigürasyon ──────────────────────────────────────────────────────────
LEVEL_CONFIG = {
    1: {"min": 0,          "max": 10_000,          "limit": 1},
    2: {"min": 10_001,      "max": 1_000_000,         "limit": 2},
    3: {"min": 1_000_001,    "max": 10_000_000,       "limit": 3},
    4: {"min": 10_000_001,  "max": 100_000_000,      "limit": 5},
    5: {"min": 100_000_001, "max": float("inf"),    "limit": 7},
}

DAYS_IN_CYCLE         = 7    # Total days in one publishing cycle
NEXT_CYCLE_OFFSET_DAYS = 6   # 1 day collection + 6 days gap = 7d cadence
UPLOAD_HOUR           = 15   # Fixed daily upload time (3 PM UTC)
MIN_VIDEO_SCORE       = 7    # temporary — lower to get first successful render with multimodal


# ═══════════════════════════════════════════════════════════════
#  FAZ 0 – Yardımcı Fonksiyonlar
# ═══════════════════════════════════════════════════════════════

# ── bg_music klasörü skelet oluşturucu ──────────────────────────────────────
BG_MUSIC_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bg_music")


def ensure_bg_music_folders():
    """
    Veritabanındaki TÜM benzersiz sektör isimlerini tarar ve
    bg_music/{sector_slug}/ klasörünü yoksa otomatik oluşturur.

    Klasör adı kuralı:
      - küçük harf
      - boşluk → alt çizgi (_)
      - ASCII olmayan karakter → çıkarılır

    Ek olarak bg_music/default/ klasörü her zaman oluşturulur
    (pick_bg_music() için fallback).
    """
    import re as _re

    os.makedirs(BG_MUSIC_BASE, exist_ok=True)

    # Daima default/ klasörünü garantile
    default_dir = os.path.join(BG_MUSIC_BASE, "default")
    os.makedirs(default_dir, exist_ok=True)

    # DB'deki tüm benzersiz sektör isimlerini çek
    try:
        sector_names = [row.name for row in session.query(Sectors.name).distinct().all()
                        if row.name]
    except Exception as e:
        print(f"[BG_MUSIC] Sektör sorgusu başarısız: {e}")
        return

    print(f"[BG_MUSIC] {len(sector_names)} benzersiz sektör bulundu: {sector_names}")

    created = []
    for name in sector_names:
        # ── Strict folder-name normaliser (must match pick_bg_music in legacy_runner.py) ──
        # Collapse any run of non-alphanumeric chars into a single '_', then
        # strip leading/trailing underscores → matches manually-created folders.
        # 'Education (Riskli)' → 'education_riskli'
        # 'Tech / AI (Riskli)' → 'tech_ai_riskli'
        slug = _re.sub(r'[^a-z0-9]+', '_', name.lower().strip()).strip('_')
        if not slug:
            continue
        folder = os.path.join(BG_MUSIC_BASE, slug)
        if not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)
            # README bırak ki kullanıcı ne koyacağını bilsin
            readme = os.path.join(folder, "README.txt")
            with open(readme, "w", encoding="utf-8") as f:
                f.write(
                    f"# {name} Sektörü – Arka Plan Müziği\n"
                    f"# Bu klasöre .mp3 / .wav / .ogg / .m4a dosyaları koyun.\n"
                    f"# Pipeline her video için buradan rastgele bir parça seçer.\n"
                    f"# Klasör adı (normalised): {slug}\n"
                )
            created.append(slug)
        else:
            print(f"[BG_MUSIC]   ✓ Mevcut: bg_music/{slug}/")

    if created:
        print(f"[BG_MUSIC] Oluşturulan yeni klasörler: {created}")
    else:
        print("[BG_MUSIC] Tüm sektör klasörleri zaten mevcut.")


def get_daily_limit(owned_channel: OwnedChannels) -> int:
    level = max(1, min(owned_channel.level or 1, 5))
    return LEVEL_CONFIG[level]["limit"]


def calculate_weekly_need(owned_channel: OwnedChannels) -> int:
    """Returns total videos needed for a 7-day cycle based on channel level."""
    return get_daily_limit(owned_channel) * DAYS_IN_CYCLE


def is_source_url_processed(url: str) -> bool:
    """
    Check via raw SQL to bypass SQLAlchemy's identity-map cache.
    The module-level session singleton can become stale between cycles;
    a text() query hits the DB engine directly every time.
    """
    from sqlalchemy import text
    result = session.execute(
        text("SELECT 1 FROM processed_source_videos WHERE video_url = :url LIMIT 1"),
        {"url": url},
    )
    return result.fetchone() is not None


def mark_source_url_processed(url: str, sector_id: int, status: str = "PROCESSED",
                               published_at=None, source_channel_id=None):
    """
    Records a source video URL as processed.
    published_at  : datetime parsed from yt-dlp upload_date (for reverse-chron ordering)
    source_channel_id : FK to Channels table (for pool exhaustion tracking)
    """
    try:
        rec = ProcessedSourceVideos(
            video_url=url,
            sector_id=sector_id,
            status=status,
            published_at=published_at,
            source_channel_id=source_channel_id,
            processed_at=datetime.datetime.utcnow(),
        )
        session.add(rec)
        session.commit()
    except Exception as e:
        print(f"[WARN] mark_source_url_processed: {e}")
        session.rollback()


def _mark_failed(url: str, sector_id: int, reason: str, source_channel_id: int = None):
    """
    Upserts a PERMANENT_DL_FAIL record for a source URL.
    If the row already exists, increments failure_count and refreshes
    last_failed_at / failure_reason. If not, inserts a fresh row.
    This ensures permanently-failed videos are NEVER retried automatically
    (is_source_url_processed() will return True on next scan).
    """
    from sqlalchemy import text as _text
    try:
        existing = session.execute(
            _text("SELECT id, failure_count FROM processed_source_videos WHERE video_url = :url"),
            {"url": url},
        ).fetchone()
        now = datetime.datetime.utcnow()
        if existing:
            session.execute(
                _text(
                    "UPDATE processed_source_videos "
                    "SET status='PERMANENT_DL_FAIL', "
                    "    failure_count = failure_count + 1, "
                    "    last_failed_at = :now, "
                    "    failure_reason = :reason "
                    "WHERE video_url = :url"
                ),
                {"now": now, "reason": reason[:500], "url": url},
            )
        else:
            rec = ProcessedSourceVideos(
                video_url=url,
                sector_id=sector_id,
                source_channel_id=source_channel_id,
                status="PERMANENT_DL_FAIL",
                processed_at=now,
                failure_count=1,
                last_failed_at=now,
                failure_reason=reason[:500],
            )
            session.add(rec)
        session.commit()
    except Exception as e:
        print(f"[WARN] _mark_failed: {e}")
        session.rollback()


def update_channel_levels():
    """Updates channel levels every Monday based on videos produced in the last 7 days."""
    if datetime.datetime.today().weekday() != 0:
        return
    print("[LEVEL] Updating channel levels ...")
    seven_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    for ch in session.query(OwnedChannels).all():
        recent_count = session.query(videos).filter(
            videos.owned_channel_id == ch.id,
            videos.created_at >= seven_days_ago
        ).count()
        new_level = 1
        for lvl, cfg in LEVEL_CONFIG.items():
            if cfg["min"] <= recent_count <= cfg["max"]:
                new_level = lvl
                break
        if ch.level != new_level:
            print(f"  Channel {ch.id}: Level {ch.level} → {new_level}  (7d videos: {recent_count})")
            ch.level = new_level
    session.commit()


# ═══════════════════════════════════════════════════════════════
#  FAZ 2 – Video Toplama
# ═══════════════════════════════════════════════════════════════

def fetch_channel_video_urls(channel_url: str, channel_db_id: int = 0,
                             max_videos: int = 50) -> list:
    """
    Fetches unprocessed video URLs from a YouTube channel.
    Returns a list of dicts sorted NEWEST-FIRST by published_at:
      [{ "url": str, "published_at": datetime|None, "source_channel_id": int }, ...]

    Diagnostic logging shows exactly how many entries yt-dlp returned, and
    why each individual entry was accepted or rejected.
    """
    base_url = channel_url.rstrip("/")
    for suffix in ["/shorts", "/featured", "/videos", "/streams", "/about"]:
        base_url = base_url.split(suffix)[0]
    videos_tab_url = base_url + "/videos"

    print(f"    [FETCH] Scanning: {videos_tab_url}  (max={max_videos})")

    # Use sys.executable + "-m yt_dlp" instead of the yt-dlp.exe binary to
    # bypass Windows Device Guard / execution policy and ensure we run under
    # the correct Python 3.11 environment.
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--match-filter", "!is_live & !is_upcoming & availability=public",
        "--playlist-end", str(max_videos),
        videos_tab_url,
    ]
    try:
        proc = subprocess.run(
            cmd, shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
        )
        if proc.returncode != 0:
            stderr_msg = proc.stderr.decode("utf-8", errors="replace").strip()[:300]
            print(f"    [FETCH] yt-dlp exited {proc.returncode}: {stderr_msg}")
            return []

        data    = json.loads(proc.stdout)
        entries = data.get("entries") or []

        total_raw       = len(entries)
        skip_no_id      = 0
        skip_processed  = 0
        skip_short      = 0
        accepted        = 0

        unprocessed = []
        for entry in entries:
            if not entry:
                skip_no_id += 1
                continue
            vid_id = entry.get("id")
            if not vid_id:
                skip_no_id += 1
                continue

            vid_url  = f"https://www.youtube.com/watch?v={vid_id}"

            # ── Deduplication check (raw SQL, bypasses identity map) ────────────
            if is_source_url_processed(vid_url):
                skip_processed += 1
                continue

            # ── Duration check ───────────────────────────────────────────────────
            # duration=None means yt-dlp didn't fetch it (flat-playlist mode).
            # In that case we ALLOW the video through — the real duration is
            # known only after download. Never skip a None-duration video.
            duration = entry.get("duration")  # can be None, int or float
            if duration is not None and duration < 60:
                print(f"    [FETCH]   SKIP (short {duration}s): {vid_url}")
                skip_short += 1
                continue

            # ── Parse publish date ───────────────────────────────────────────────
            pub_dt   = None
            raw_date = entry.get("upload_date")   # "YYYYMMDD" string
            raw_ts   = entry.get("timestamp")     # Unix timestamp (backup field)
            if isinstance(raw_date, str) and len(raw_date) == 8:
                try:
                    pub_dt = datetime.datetime.strptime(raw_date, "%Y%m%d")
                except ValueError:
                    pass
            elif isinstance(raw_ts, (int, float)) and raw_ts:
                pub_dt = datetime.datetime.utcfromtimestamp(raw_ts)

            accepted += 1
            unprocessed.append({
                "url":               vid_url,
                "published_at":      pub_dt,
                "source_channel_id": channel_db_id,
            })

        # Explicit newest-first sort (double guarantee on top of yt-dlp default)
        unprocessed.sort(
            key=lambda x: x["published_at"] or datetime.datetime.min,
            reverse=True,
        )

        print(
            f"    [FETCH] yt-dlp returned {total_raw} entries → "
            f"accepted={accepted}  skip_processed={skip_processed}  "
            f"skip_short={skip_short}  skip_no_id={skip_no_id}  "
            f"(newest-first order)"
        )
        return unprocessed

    except subprocess.TimeoutExpired:
        print(f"    [FETCH] TIMEOUT after 90s: {videos_tab_url}")
        return []
    except json.JSONDecodeError as e:
        print(f"    [FETCH] JSON parse error: {e}")
        return []
    except Exception as e:
        print(f"    [FETCH] Error: {e}")
        return []


def process_video_to_shorts(
    source_url: str,
    sector: Sectors,
    source_channel_id: int,
    owned_channel: OwnedChannels,
    published_at=None,             # datetime|None – from fetch_channel_video_urls
    total_target: int = None,      # Target needed for the channel
) -> tuple[int, bool]:
    """
    Downloads source video, splits into segments, uses Qwen to identify viral
    clips, generates structured AI metadata, creates Kokoro TTS voiceover, and
    renders shorts with info-box + subscribe button + TTS audio.

    GPU assignment (enforced inside legacy_runner.py):
      cuda:0 → Qwen2.5-14B-Instruct-AWQ + Kokoro-82M TTS
      cuda:1 → Whisper transcription + h264_nvenc encoding + CUDA blur

    All rendering assets (voice_id, theme_color, channel_name) are read
    directly from the owned_channel object.

    Returns: number of shorts successfully produced.
    """
    print(f"\n    [PROCESS] Processing: {source_url}")

    # ── 1. Download ──────────────────────────────────────────────────────────
    # Phrases that indicate a video is private / Members-Only – skip silently.
    _SKIP_HINTS = (
        "members only", "private video", "video is private",
        "join this channel", "subscribers only",
        "this video is available to", "login required",
    )

    MAX_DL_RETRY = 3
    video_path   = None

    for dl_attempt in range(1, MAX_DL_RETRY + 1):
        try:
            video_path = legacy_runner.download_youtube_video(source_url)
            break   # success — exit retry loop
        except Exception as e:
            err_lower = str(e).lower()
            if any(hint in err_lower for hint in _SKIP_HINTS):
                print(f"    [SKIP] Members-Only / private video: {source_url}")
                mark_source_url_processed(source_url, sector.id, status="SKIPPED_PRIVATE")
                return 0, False
            print(f"    [DL RETRY {dl_attempt}/{MAX_DL_RETRY}] {e}")
            if dl_attempt == MAX_DL_RETRY:
                err_str = str(e)[:500]
                msg = (
                    f"Download failed after {MAX_DL_RETRY} attempts: "
                    f"{source_url}\nLast error: {e}"
                )
                print(f"    [SKIP] Video skipped after {MAX_DL_RETRY} failures: {source_url} | Reason: {err_str}")
                print(f"    [ERROR] {msg}")
                # Write failure details to DB row
                _mark_failed(source_url, sector.id, err_str, source_channel_id)
                send_alert(f"Permanent Download Fail – Sector: {sector.name}", msg)
                return 0, False
            # Clear stale yt-dlp cache ONLY after a failure, before next retry
            print(f"    [DL] Clearing yt-dlp cache before retry {dl_attempt + 1} ...")
            import subprocess as _sp
            _sp.run([sys.executable, "-m", "yt_dlp", "--rm-cache-dir"], check=False,
                    capture_output=True)
            time.sleep(5)   # brief pause before retry

    if not video_path:
        return 0, False

    # ── 2. Split into segments ───────────────────────────────────────────────
    try:
        if not os.path.exists(video_path) or os.path.getsize(video_path) < 1000000:
            raise Exception("Download failed: File is missing or too small.")
        segments = legacy_runner.split_video_into_segments(video_path)
        if not segments:
            raise ValueError("No segments produced")
    except Exception as e:
        print(f"    [ERROR] Split error: {e}")
        mark_source_url_processed(source_url, sector.id, status="FAILED_SPLIT")
        return 0

    sector_dir = os.path.join("shorts", f"sector_{sector.id}")
    os.makedirs(sector_dir, exist_ok=True)

    # Resolve owned_channel creative assets (DB-driven)
    ch_name        = owned_channel.name or f"Channel {owned_channel.id}"
    voice_id       = owned_channel.voice_id or "af_sky"

    persona        = getattr(owned_channel, "persona",        "") or ""
    font_style     = getattr(owned_channel, "font_style",     "Impact") or "Impact"
    accent_color_1 = getattr(owned_channel, "accent_color_1", "#1E90FF") or "#1E90FF"
    accent_color_2 = getattr(owned_channel, "accent_color_2", "#FF6D00") or "#FF6D00"
    sfx_set        = getattr(owned_channel, "sfx_set",        "default") or "default"

    generated_count = 0

    # ── Direct-Instruction scoring prompt (no chain-of-thought) ─────────────
    # Removing 'analyze deeply'/'think step-by-step' cuts Qwen inference ~40%.
    # 3-min segments + 1500-word cap = target <90 s per Qwen scoring call.
    system_prompt = """\
You are a viral clip extractor. Scan the transcript and output ONLY clip timestamps.
No analysis. No explanation. No prose.

SCORING (0-9):
0-6 = boring/skip (conversations, intros, filler). NEVER include these.
7   = mildly interesting moment.
8   = high-energy, surprising, or funny moment.
9   = TOP 1% ONLY: shock, scream, massive fail, hysteria.

OUTPUT FORMAT — one line per clip, nothing else:
INDEX/SCORE/START - END
Example: 1/8/00:15 - 00:37

Rules: duration 20-25 s. Top 3 clips. Zero extra text."""

    target_reached = False
    for seg_path in segments:
        if target_reached:
            break
        try:
            # ── VRAM Kill-Switch: flush cuda:0 before each segment's AI stage ──
            # Single-GPU mode: one card handles everything, so we only flush
            # cuda:0 and confirm it's clean before loading the next model.
            legacy_runner.cleanup_gpu()  # gc.collect() + empty_cache() on cuda:0

            # ── 2a. Transcribe segment (cuda:0) ───────────────────────────────
            transcript = legacy_runner.generate_segment_transcript(seg_path)
            if not transcript:
                continue

            # Hard-cap transcript before Qwen (prevents OOM on 12 GB RTX 3060)
            safe_transcript = legacy_runner._truncate_transcript(transcript)
            user_prompt = (
                f"Transcript:\n{safe_transcript}\n\n"
                f"Find the top 3 viral clips (20-25 s each)."
            )
            messages    = [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ]

            found_intervals    = []
            target_per_segment = 3

            for attempt in range(3):
                if target_reached or len(found_intervals) >= target_per_segment:
                    break

                try:
                    reply = legacy_runner.send_prompt_to_qwen(messages, max_tokens=1500)                
                except Exception as e:
                    print(f"    [ERROR] Qwen error: {e}")
                    break

                for line in reply.splitlines():
                    if target_reached or len(found_intervals) >= target_per_segment:
                        break

                    match = re.search(
                        r"(\d+)/(\d+)/(\d+[:\.]\d+)\s*-\s*(\d+[:\.]\d+)", line
                    )
                    if not match:
                        continue

                    score = int(match.group(2))
                    start = legacy_runner.parse_timecode(match.group(3))
                    end   = legacy_runner.parse_timecode(match.group(4))
                    dur   = end - start

                    # ── Strict quality gate ────────────────────────────────
                    if score < MIN_VIDEO_SCORE:
                        print(f"    [QUALITY] Rejected clip: score={score} < {MIN_VIDEO_SCORE}")
                        continue

                    if not (20 <= dur <= 25):
                        continue
                    if legacy_runner.is_overlapping(start, found_intervals):
                        continue

                    try:
                        _video_start = time.time()
                        spec = {"start": start, "end": end, "score": score}

                        # ── 2b. Qwen 4-part script (GPU 0) ────────────────
                        clip_transcript_snippet = _extract_transcript_slice(
                            transcript, start, end
                        )
                        
                        # Extract audio and word segments for multimodal context
                        import uuid
                        clip_audio_path = os.path.join(legacy_runner.SHORTS_DIR, f"tmp_clip_{uuid.uuid4().hex[:8]}.mp3")
                        subprocess.run([
                            "ffmpeg", "-y", "-ss", str(start), "-to", str(end),
                            "-i", seg_path, "-vn", "-c:a", "libmp3lame", "-q:a", "4", clip_audio_path
                        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        
                        whisper_word_segments = legacy_runner.transcribe_subclip_for_karaoke(clip_audio_path)
                        
                        ai_meta = legacy_runner.generate_script_with_qwen(
                            clip_transcript_snippet or transcript,
                            sector.name,
                            persona=persona,
                            video_path=seg_path,
                            clip_start=start,
                            clip_end=end,
                            audio_path=clip_audio_path,
                            word_segments=whisper_word_segments,
                            source_channel_handle=owned_channel.name
                        )

                        # Immediately sweep VRAM space so render pipeline has room
                        legacy_runner.cleanup_gpu()

                        # ── 2c. Kokoro TTS: judgment + outro_question merged ─
                        # The judgment and outro_question are the voiced portion
                        tts_path    = None
                        judgment    = ai_meta.get("judgment", "")
                        outro_q     = ai_meta.get("outro_question", "")
                        # Merge with a natural pause token
                        voiced_text = " ".join(
                            part for part in [judgment, outro_q] if part
                        ).strip()

                        if voiced_text:
                            tts_filename = os.path.join(
                                legacy_runner.TTS_DIR,
                                f"tts_{owned_channel.id}_{uuid_hex()}.wav"
                            )
                            success = legacy_runner.generate_kokoro_tts(
                                voiced_text, voice_id, tts_filename
                            )
                            if success:
                                tts_path = tts_filename

                        # ── 2d. IG Reels 4-Stage Pipeline ────────────────────
                        # Resolve original handle for attribution overlay
                        _pool_url    = getattr(pool_ch, "url", "") if "pool_ch" in dir() else ""
                        _orig_handle = _pool_url.split("/@")[-1].split("/")[0] if "/@" in _pool_url else _pool_url

                        from ffmpeg_renderer import render_raw_ffmpeg, burn_overlays_ffmpeg
                        import rife_interpolator

                        # -- Stage 1: Raw render (1080x1920 @ 24fps) ----------
                        raw_path, ass_path, clip_dur, render_elapsed = render_raw_ffmpeg(
                            seg_path, spec, generated_count,
                            sector_name=sector.name,
                            original_handle=_orig_handle,
                            _transcribe_fn=legacy_runner.transcribe_subclip_for_karaoke,
                            _pick_bgm_fn=legacy_runner.pick_bg_music,
                        )

                        # -- Stage 2: RIFE 3x interpolation (-> 72fps) --------
                        legacy_runner.cleanup_gpu()
                        rife_out_path = raw_path.replace(".mp4", "_72fps.mp4")
                        rife_ok = rife_interpolator.apply_rife_interpolation(
                            input_mp4=raw_path,
                            output_mp4=rife_out_path,
                        )
                        if not rife_ok:
                            logger.error(
                                "RIFE interpolation FAILED for %s — aborting pipeline. "
                                "Raw render preserved at %s for inspection.",
                                os.path.basename(raw_path), raw_path,
                            )
                            raise RuntimeError(
                                f"RIFE interpolation failed for {os.path.basename(raw_path)}"
                            )

                        # -- Stage 3: Overlay burn-in + final NVENC encode ----
                        final_path = raw_path.replace(".mp4", "_final.mp4")
                        overlay_ok = burn_overlays_ffmpeg(
                            video_path=rife_out_path,
                            audio_path=raw_path,       # audio source
                            ass_path=ass_path,
                            clip_duration=clip_dur,
                            output_path=final_path,
                            accent_color_1=accent_color_1,
                        )
                        if not overlay_ok:
                            logger.error(
                                "burn_overlays FAILED for %s — aborting pipeline. "
                                "RIFE output preserved at %s for inspection.",
                                os.path.basename(rife_out_path), rife_out_path,
                            )
                            raise RuntimeError(
                                f"burn_overlays failed for {os.path.basename(rife_out_path)}"
                            )

                        # All 3 stages succeeded — swap final into production path
                        if os.path.exists(final_path):
                            os.replace(final_path, raw_path)
                        # Cleanup intermediates
                        for _tmp_f in [rife_out_path]:
                            if os.path.exists(_tmp_f):
                                try: os.remove(_tmp_f)
                                except: pass

                        
                        # ── 2e. Persist to DB ──────────────────────────────
                        _raw_tags = ai_meta.get("hashtags", [])
                        _hashtags_str = ", ".join(_raw_tags) if isinstance(_raw_tags, list) else str(_raw_tags or "")
                        new_vid = videos(
                            sector_id=sector.id,
                            owned_channel_id=owned_channel.id,
                            qwen_point=score,
                            real_point=0,
                            channel_id=source_channel_id,
                            source_channel_id=source_channel_id,
                            # AI-generated metadata (4-part script)
                            title=ai_meta.get("title"),
                            summary_text=ai_meta.get("judgment", ""),

                            tts_path=tts_path,
                            caption=ai_meta.get("caption", ""),
                            hashtags=_hashtags_str,
                        )
                        new_vid.render_time_seconds = render_elapsed
                        session.add(new_vid)
                        session.commit()

                        _video_elapsed = time.time() - _video_start
                        new_vid.render_time_seconds = _video_elapsed
                        session.commit()
                        print(f"    [TIMER] Total pipeline: {_video_elapsed:.0f}s ({_video_elapsed/60:.1f} min)")

                        final_path = os.path.join(sector_dir, f"video_{new_vid.id}.mp4")
                        try:
                            os.rename(raw_path, final_path)
                        except OSError:
                            shutil.move(raw_path, final_path)

                        found_intervals.append(start)
                        generated_count += 1
                        print(f"    [SHORT] ✓ video_{new_vid.id}.mp4  (score: {score})")

                        if total_target is not None:
                            current_count = session.query(videos).filter_by(
                                owned_channel_id=owned_channel.id,
                                is_used=False
                            ).count()
                            if current_count >= total_target:
                                print(f"    [COLLECT] Target reached ({current_count}/{total_target}), stopping.")
                                target_reached = True
                                break

                    except Exception as e:
                        print(f"    [RENDER FAIL] {e}")
                        traceback.print_exc()

        except Exception as e:
            print(f"    [ERROR] Segment processing error: {e}")

    status = "PROCESSED_SUCCESS" if generated_count > 0 else "PROCESSED_NO_CLIPS"
    mark_source_url_processed(
        source_url, sector.id, status=status,
        published_at=published_at,
        source_channel_id=source_channel_id,
    )

    # ── Quality summary ───────────────────────────────────────────────────────
    if total_target is not None:
        sess_q = Session()
        filled = sess_q.query(videos).filter_by(
            owned_channel_id=owned_channel.id, is_used=False
        ).count()
        sess_q.close()
        ch_name_q = owned_channel.name or f"Channel {owned_channel.id}"
        if filled >= total_target:
            print(f"  [QUALITY] Channel {ch_name_q}: filled {filled}/{total_target} with score={MIN_VIDEO_SCORE} clips ✓")
        else:
            print(f"  [QUALITY] Channel {ch_name_q}: only filled {filled}/{total_target} with score={MIN_VIDEO_SCORE} clips this cycle")

    print(f"    [PROCESS] Done → {generated_count} shorts produced.")
    return generated_count, target_reached


def _extract_transcript_slice(transcript: str, start: float, end: float,
                               margin: float = 5.0) -> str:
    """
    Extracts transcript lines that fall within [start-margin, end+margin].
    Used to give Qwen focused context for the metadata analysis prompt.
    """
    lines  = transcript.splitlines()
    result = []
    tc_re  = re.compile(r"\[(\d+:\d+)\s*-\s*(\d+:\d+)\]")

    for line in lines:
        m = tc_re.search(line)
        if not m:
            continue
        seg_start = legacy_runner.parse_timecode(m.group(1))
        seg_end   = legacy_runner.parse_timecode(m.group(2))
        if seg_end >= (start - margin) and seg_start <= (end + margin):
            result.append(line)

    return "\n".join(result)


def uuid_hex(n: int = 8) -> str:
    import uuid
    return uuid.uuid4().hex[:n]


def collect_videos_for_sector(
    sector: Sectors,
    total_needed: int,
    owned_channels_for_sector: list,   # list[OwnedChannels]
) -> int:
    """
    Gap-First Sequential Sourcing — the production-disciplined replacement
    for the old round-robin loop.

    For each OwnedChannel in the sector:
      1. Calculate the channel's personal gap (30-day window vs. ready videos).
      2. Pick the FIRST pool channel from the sector.
      3. Extract as many shorts as possible from its latest videos.
      4. Subtract produced shorts from the gap.
      5. If gap > 0 move to the NEXT pool channel and repeat.
      6. If gap == 0 STOP for this OwnedChannel (no over-production).

    This guarantees we never waste GPU cycles on channels that already
    have enough content.

    Returns: total number of new shorts generated this run.
    """
    from fulfillment_engine import get_channel_inventory

    print(f"\n  [COLLECT] Sector '{sector.name}' — starting Gap-First sourcing")

    source_channels = sector.channels
    if not source_channels:
        msg = f"No source channels for sector '{sector.name}'! Add channels to the pool."
        print(f"  [WARN] {msg}")
        send_alert(f"Missing Source Channel – {sector.name}", msg)
        return 0

    print(f"  [COLLECT] Pool has {len(source_channels)} source channel(s): "
          f"{[ch.url for ch in source_channels]}")

    grand_total_generated = 0

    for owned_ch in owned_channels_for_sector:
        # ── Compute this channel's individual gap ────────────────────────────
        sess = Session()
        try:
            inv = get_channel_inventory(sess, owned_ch)
        finally:
            sess.close()

        # If fulfillment mode explicitly provided a total_needed, use it to override the inventory check
        if total_needed is not None:
            current = session.query(videos).filter_by(owned_channel_id=owned_ch.id, is_used=False).count()
            gap = max(total_needed - current, 0)
        else:
            gap = inv["gap"]
            
        ch_name = owned_ch.name or f"Channel {owned_ch.id}"

        if gap == 0:
            print(f"  [COLLECT] Channel '{ch_name}': gap=0 — already full, skipping.")
            continue

        print(f"  [COLLECT] Channel '{ch_name}': gap={gap} — sourcing from pool...")

        channel_total_generated = 0
        remaining_gap = gap

        # ── Round-robin pool-channel traversal ────────────────────────────────
        from itertools import zip_longest
        
        # 1. Fetch all pool channel URL lists first
        print(f"  [COLLECT] Fetching all pool channel URLs first...")
        all_channel_urls = []
        for pool_ch in source_channels:
            url_dicts = fetch_channel_video_urls(
                pool_ch.url, channel_db_id=pool_ch.id, max_videos=50
            )
            
            if not url_dicts:
                url_dicts = fetch_channel_video_urls(
                    pool_ch.url, channel_db_id=pool_ch.id, max_videos=200
                )
                
            if not url_dicts:
                pool_exhaustion_msg = (
                    f"SOURCE_POOL_EXHAUSTED: No unprocessed videos remain "
                    f"for pool channel '{pool_ch.url}' (sector: {sector.name}). "
                    f"Add more channels to the pool or wait for new uploads."
                )
                print(f"  [WARN] {pool_exhaustion_msg}")
                exhaustion_log = logs(
                    sector_id=sector.id,
                    messages=pool_exhaustion_msg,
                    uploaded_at=datetime.datetime.utcnow(),
                )
                session.add(exhaustion_log)
                session.commit()
                continue
                
            # fetch_channel_video_urls already filters out processed videos
            all_channel_urls.append((pool_ch, url_dicts))

        # 2. Interleave round-robin
        interleaved = []
        for group in zip_longest(*[urls for _, urls in all_channel_urls]):
            for i, vid_info in enumerate(group):
                if vid_info is not None:
                    interleaved.append((all_channel_urls[i][0], vid_info))

        print(f"  [COLLECT] Round-robin interleaved: {len(interleaved)} total URLs")

        # 3. Process interleaved list
        consecutive_failures = 0
        MAX_CONSECUTIVE_FAIL = 15 # increased limit for interleaved sourcing
        
        for pool_ch, vid_info in interleaved:
            if remaining_gap <= 0:
                print(f"  [COLLECT]   Gap filled for '{ch_name}'. Moving to next OwnedChannel.")
                break

            current = session.query(videos).filter_by(
                owned_channel_id=owned_ch.id,
                is_used=False
            ).count()
            if total_needed is not None and current >= total_needed:
                print(f"  [COLLECT] Already at target ({current}/{total_needed}), done.")
                break

            print(f"  [COLLECT]   Attempting: {vid_info['url']} from pool channel: {pool_ch.url}")

            try:
                count, target_hit = process_video_to_shorts(
                    vid_info["url"],
                    sector,
                    pool_ch.id,
                    owned_ch,
                    published_at=vid_info["published_at"],
                    total_target=total_needed,
                )
            except Exception as fatal_e:
                print(f"  [FATAL] Uncaught error processing {vid_info['url']}: {fatal_e}")
                import traceback
                traceback.print_exc()
                mark_source_url_processed(
                    vid_info["url"], sector.id, status="FAILED_FATAL_ERROR", 
                    source_channel_id=pool_ch.id, published_at=vid_info["published_at"]
                )
                count = 0
                target_hit = False

            channel_total_generated += count
            grand_total_generated   += count

            if count > 0:
                consecutive_failures = 0
                remaining_gap -= count
                session.expire_all()
                print(
                    f"  [COLLECT]   +{count} shorts produced. "
                    f"gap remaining={remaining_gap}"
                )
            else:
                consecutive_failures += 1
                print(
                    f"  [COLLECT]   0 clips (consecutive_failures="
                    f"{consecutive_failures}/{MAX_CONSECUTIVE_FAIL})"
                )
                print(
                    f"  [STATS] Channel {ch_name}: "
                    f"{consecutive_failures}/{MAX_CONSECUTIVE_FAIL} consecutive failures"
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAIL:
                    cooldown_until = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
                    print(
                        f"  [COOLDOWN] Channel {ch_name} on cooldown until "
                        f"{cooldown_until.strftime('%H:%M:%S UTC')}"
                    )
                    print(
                        f"  [COLLECT]   {MAX_CONSECUTIVE_FAIL} consecutive zero-clip "
                        f"videos. GPU pipeline may be down. Stopping collection."
                    )
                    break

            if target_hit:
                print(f"  [COLLECT] Channel target reached, stopping collection.")
                break

        # ── Summary for this OwnedChannel ─────────────────────────────────────
        if remaining_gap > 0:
            msg = (
                f"SOURCE_POOL_EXHAUSTED: Channel ‘{ch_name}’ still needs "
                f"{remaining_gap} more video(s). All pool channels tried. "
                f"Add more pool channels to sector ‘{sector.name}’."
            )
            print(f"  [WARN] {msg}")
            send_alert(
                f"Pool Exhausted – Channel {owned_ch.id}",
                f"Gap remaining: {remaining_gap}. Add more pool channels.",
            )
        else:
            print(f"  [COLLECT] Channel ‘{ch_name}’: gap fully filled ✓")

    print(
        f"\n  [COLLECT] ┳ Sector ‘{sector.name}’ DONE ┳  "
        f"grand_total_generated={grand_total_generated}"
    )
    return grand_total_generated


# ═══════════════════════════════════════════════════════════════
#  FAZ 3 – Upload Kuyruğu Oluşturma
# ═══════════════════════════════════════════════════════════════

def build_upload_schedule(owned_channel: OwnedChannels,
                          start_datetime: datetime.datetime,
                          cycle_id: int) -> int:
    """
    Distributes available videos across 7 days (1+ per day based on level).
    Creates UploadQueue records with precise scheduled_for datetimes.
    Returns: number of queue entries created.
    """
    daily_limit    = get_daily_limit(owned_channel)
    interval_hours = 24.0 / daily_limit
    weekly_need    = calculate_weekly_need(owned_channel)
    print(f"[SCHEDULE DEBUG] channel={owned_channel.id} sector={owned_channel.sector_id} weekly_need={weekly_need} cycle_id={cycle_id}")

    # Already-queued video IDs for this cycle
    queued_ids = [
        q[0] for q in
        session.query(UploadQueue.video_id)
               .filter_by(owned_channel_id=owned_channel.id)
               .filter(UploadQueue.cycle_id == cycle_id)
               .all()
        if q[0] is not None
    ]

    # Available unqueued videos owned by this channel, ranked by AI score
    q = session.query(videos).filter_by(owned_channel_id=owned_channel.id, is_used=False)
    if queued_ids:
        q = q.filter(~videos.id.in_(queued_ids))
    available = q.order_by(videos.qwen_point.desc()).limit(weekly_need).all()
    print(f"[SCHEDULE DEBUG] available videos found: {len(available)}")
    print(f"[SCHEDULE DEBUG] queued_ids: {queued_ids}")

    if not available:
        print(f"    [SCHEDULE] No videos available for channel {owned_channel.id}.")
        return 0

    print(
        f"    [SCHEDULE] Channel {owned_channel.id} | "
        f"Level {owned_channel.level} | "
        f"Daily {daily_limit} | "
        f"Scheduling {len(available)} videos across 7 days ..."
    )

    # Fixed upload hour — always start from tomorrow to avoid past slots
    tomorrow         = start_datetime + datetime.timedelta(days=1)
    base_dt          = tomorrow.replace(hour=UPLOAD_HOUR, minute=0, second=0, microsecond=0)
    max_schedule_time = base_dt + datetime.timedelta(days=DAYS_IN_CYCLE)
    count            = 0

    for i, vid in enumerate(available):
        day_offset     = i // daily_limit
        if day_offset >= 7:
            print(f"    [SCHEDULE] 7-day limit reached at video #{i}. Stopping.")
            break
        slot_in_day    = i % daily_limit
        scheduled_time = base_dt + datetime.timedelta(
            days=day_offset,
            hours=slot_in_day * interval_hours,
        )

        if scheduled_time > max_schedule_time:
            print(f"    [SCHEDULE] 7-day window exceeded at video #{i}. Stopping.")
            break

        entry = UploadQueue(
            owned_channel_id=owned_channel.id,
            video_id=vid.id,
            scheduled_for=scheduled_time,
            status="PENDING",
            created_at=datetime.datetime.utcnow(),
            cycle_id=cycle_id,
        )
        session.add(entry)
        count += 1

    session.commit()
    print(f"[SCHEDULE DEBUG] committed {count} rows to upload_queue")

    # Print schedule summary
    print(f"    [SCHEDULE] ✓ {count} entries created.")
    if count > 0:
        first_dt = base_dt.strftime("%Y-%m-%d %H:%M UTC")
        last_dt  = (base_dt + datetime.timedelta(days=(count - 1) // daily_limit)).strftime("%Y-%m-%d")
        print(f"    [SCHEDULE] Window: {first_dt} → {last_dt}")

    return count


# ═══════════════════════════════════════════════════════════════
#  FAZ 4 – YouTube'a Zamanlanmış Yükleme
# ═══════════════════════════════════════════════════════════════

def execute_upload_queue(cycle_id: int):
    """
    Uploads all PENDING queue items for this cycle to YouTube.
    Uses the AI-generated title from the videos table if available,
    falling back to the sector title template.
    Returns: (success_count, fail_count)
    """
    publisher = YouTubePublisher(headless=False)

    pending = (
        session.query(UploadQueue)
               .filter_by(status="PENDING", cycle_id=cycle_id)
               .order_by(UploadQueue.scheduled_for)
               .all()
    )

    if not pending:
        print("[UPLOAD] No pending items in queue.")
        return 0, 0

    print(f"\n[UPLOAD] Uploading {len(pending)} videos ...")

    success_count = 0
    fail_count    = 0

    for item in pending:
        session.expire_all()

        owned_channel = session.query(OwnedChannels).get(item.owned_channel_id)
        video         = session.query(videos).get(item.video_id)

        if not owned_channel or not video:
            item.status        = "FAILED"
            item.error_message = "DB record not found"
            session.commit()
            fail_count += 1
            continue

        sector = session.query(Sectors).get(owned_channel.sector_id)

        # Resolve video file path
        sector_dir = os.path.join("shorts", f"sector_{owned_channel.sector_id}")
        file_path  = os.path.join(sector_dir, f"video_{video.id}.mp4")

        if not os.path.exists(file_path):
            err = f"File not found: {file_path}"
            print(f"  [ERROR] {err}")
            item.status        = "FAILED"
            item.error_message = err
            session.commit()
            fail_count += 1
            send_alert(
                f"Missing File – Channel {owned_channel.id}",
                f"{err}\nVideo ID: {video.id}",
            )
            continue

        # Use AI-generated title if available, otherwise fall back to sector template
        ai_title  = video.title
        sec_title = sector.title if sector else "Video"
        title = (
            ai_title
            if ai_title
            else f"{sec_title} #{video.qwen_point}"
        )

        # Build description: AI caption first, fallback to sector template
        if video.caption:
            desc_lines = [video.caption]
            if sector and sector.description:
                desc_lines.append(f"\n{sector.description[:200]}")
        else:
            desc_lines = [sector.description if sector else ""]
            if video.summary_text:
                desc_lines.append(f"\n{video.summary_text}")


        # Append hashtags to description footer
        if video.hashtags:
            desc_lines.append(f"\n{video.hashtags}")

        description = "\n".join(desc_lines).strip()

        # Tags: AI hashtags first, fallback to sector tags
        if video.hashtags:
            tag_list = [t.strip().strip("#") for t in video.hashtags.split(",") if t.strip()]
            tags = ", ".join(tag_list[:15])
        else:
            tags = sector.tags if sector else ""
        user_agent = (
            getattr(owned_channel, 'user_agent', None)
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        scheduled_local = item.scheduled_for
        print(
            f"\n  [UPLOAD] Video {video.id} → Channel {owned_channel.id} "
            f"({owned_channel.name or 'Unnamed'})"
        )
        print(f"  [UPLOAD] Title: {title}")
        print(f"  [UPLOAD] Scheduled: {scheduled_local.strftime('%Y-%m-%d %H:%M UTC')}")

        try:
            success = publisher.upload_video(
                video_path=os.path.abspath(file_path),
                title=title,
                description=description,
                tags=tags,
                js_path=getattr(owned_channel, 'js_path', None),
                user_agent=user_agent,
                schedule_datetime=scheduled_local,
            )

            if success:
                item.status      = "UPLOADED"
                item.uploaded_at = datetime.datetime.utcnow()
                video.is_used    = True

                new_log = logs(
                    sector_id=sector.id if sector else None,
                    owned_channel_id=owned_channel.id,
                    video_id=video.id,
                    messages=f"Scheduled: {scheduled_local.strftime('%Y-%m-%d %H:%M')} | Title: {title}",
                    uploaded_at=datetime.datetime.utcnow(),
                )
                session.add(new_log)
                session.commit()

                success_count += 1
                print(f"  [UPLOAD] ✓ Success!")

            else:
                item.status        = "FAILED"
                item.error_message = "Publisher returned False"
                item.retry_count   = (item.retry_count or 0) + 1
                session.commit()
                fail_count += 1

                send_alert(
                    f"Upload Failed – Channel {owned_channel.id}",
                    f"Upload failed!\nVideo ID: {video.id}\n"
                    f"Channel: {owned_channel.name}\n"
                    f"Scheduled: {scheduled_local.strftime('%Y-%m-%d %H:%M')}",
                )

        except Exception as e:
            tb = traceback.format_exc()
            print(f"  [ERROR] Upload exception: {e}")
            item.status        = "FAILED"
            item.error_message = str(e)[:500]
            item.retry_count   = (item.retry_count or 0) + 1
            session.commit()
            fail_count += 1

            send_alert(
                f"Upload Error – Channel {owned_channel.id}",
                f"Exception during upload!\nError: {e}\n"
                f"Video ID: {video.id}\n\nTraceback:\n{tb}",
            )

        time.sleep(5)   # brief pause between uploads (anti-bot protection)

    print(
        f"\n[UPLOAD] Phase complete → "
        f"✓ {success_count} successful, ✗ {fail_count} failed"
    )
    return success_count, fail_count


# ═══════════════════════════════════════════════════════════════
#  FAZ 5 – Temizlik
# ═══════════════════════════════════════════════════════════════

def cleanup_reels_folder():
    """Removes all .mp4 files from the reels/ directory."""
    print("\n[CLEANUP] Clearing reels folder ...")
    if not os.path.exists("reels"):
        return

    deleted = errors = 0
    for root, dirs, files in os.walk("reels"):
        for filename in files:
            if filename.endswith(".mp4"):
                filepath = os.path.join(root, filename)
                try:
                    os.remove(filepath)
                    deleted += 1
                except Exception as e:
                    print(f"  [WARN] Could not delete: {filepath} -- {e}")
                    errors += 1

    print(f"[CLEANUP] Deleted: {deleted} files, Errors: {errors}")

# ═══════════════════════════════════════════════════════════════
#  ANA DÖNGÜ
# ═══════════════════════════════════════════════════════════════

def main():
    print("\n" + "═" * 65)
    print("  UTube Bot – Weekly Cycle System Starting")
    print("═" * 65 + "\n")

    # ── bg_music klasör iskeletini DB'deki sektörlere göre hazırla ──────────
    ensure_bg_music_folders()

    import update_stats

    cycle_number = 0

    while True:
        cycle_number += 1
        cycle_start   = datetime.datetime.utcnow()
        cycle_id      = int(cycle_start.timestamp())

        print(f"\n{'═'*65}")
        print(
            f"  CYCLE #{cycle_number} STARTING  "
            f"[{cycle_start.strftime('%Y-%m-%d %H:%M UTC')}]"
        )
        print(f"{'═'*65}\n")

        success_count = 0
        fail_count    = 0
        total_queued  = 0

        try:
            # ── FAZ 0: Stats & Levels ────────────────────────────────────────
            print("[PHASE 0] Updating stats ...")
            try:
                update_stats.update_all_channels()
            except Exception as e:
                print(f"  [WARN] Stats update error: {e}")
            update_channel_levels()

            # ── FAZ 1: Monthly Summary ────────────────────────────────────────
            owned_channels = session.query(OwnedChannels).all()

            if not owned_channels:
                msg = "No owned channels found! Add them via the web panel."
                print(f"[WARN] {msg}")
                send_alert("No Owned Channels", msg)
                time.sleep(3600)
                continue

            print("\n[PHASE 1] Weekly Requirement Summary:")
            print(f"  {'Channel':<28} {'Lv':<5} {'Daily':<8} {'Weekly Need'}")
            print("  " + "─" * 55)
            for oc in owned_channels:
                daily  = get_daily_limit(oc)
                weekly = calculate_weekly_need(oc)
                name   = (oc.name or f"Channel {oc.id}")[:26]
                print(f"  {name:<28} {oc.level:<5} {daily:<8} {weekly}")

            # ── FAZ 2: Sector-based Video Collection ─────────────────────────
            print("\n[PHASE 2] Starting video collection ...")

            # Build sector → {owned_channels} map for asset resolution
            sector_map: dict[int, dict] = {}
            for oc in owned_channels:
                sec = session.query(Sectors).get(oc.sector_id)
                if not sec:
                    continue
                need = calculate_weekly_need(oc)
                if sec.id not in sector_map:
                    sector_map[sec.id] = {
                        "sector":          sec,
                        "total_needed":    need,
                        "owned_channels":  [oc],
                    }
                else:
                    sector_map[sec.id]["total_needed"] = max(
                        sector_map[sec.id]["total_needed"], need
                    )
                    sector_map[sec.id]["owned_channels"].append(oc)

            for sector_id, data in sector_map.items():
                try:
                    collect_videos_for_sector(
                        data["sector"],
                        data["total_needed"],
                        data["owned_channels"],   # ← DB-driven identity list
                    )
                except Exception as e:
                    tb = traceback.format_exc()
                    print(f"  [ERROR] Collection error (Sector {sector_id}): {e}")
                    send_alert(
                        f"Collection Error – Sector {data['sector'].name}",
                        f"Video collection failed!\n"
                        f"Sector: {data['sector'].name}\n"
                        f"Error: {e}\n\n{tb}",
                    )

                session.expire_all()

            # ── FAZ 3: Build Upload Schedule (7 days) ─────────────────────────
            print("\n[PHASE 3] Building 7-day upload schedule ...")
            schedule_start = datetime.datetime.utcnow()

            for oc in owned_channels:
                try:
                    count = build_upload_schedule(oc, schedule_start, cycle_id)
                    total_queued += count
                except Exception as e:
                    print(f"  [ERROR] Schedule error (Channel {oc.id}): {e}")
                    send_alert(
                        f"Schedule Error – Channel {oc.id}",
                        f"Could not build upload schedule!\n"
                        f"Channel: {oc.name}\nError: {e}",
                    )

            if total_queued == 0:
                msg = (
                    "No videos queued! Ensure the pool has source channels "
                    "and enough unprocessed content."
                )
                print(f"\n[WARN] {msg}")
                send_alert("Empty Queue", msg)
                time.sleep(3600)
                continue

            print(f"[PHASE 3] Total queued: {total_queued} videos")

            # ── FAZ 4: Upload to YouTube ──────────────────────────────────────
            print("\n[PHASE 4] Uploading to YouTube (scheduled mode) ...")
            success_count, fail_count = execute_upload_queue(cycle_id)

        except Exception as e:
            tb = traceback.format_exc()
            print(f"\n[CRITICAL ERROR] Cycle #{cycle_number} unexpected failure!")
            print(tb)
            send_alert(
                f"CRITICAL ERROR – Cycle #{cycle_number}",
                f"Main loop critical error!\nError: {e}\n\nTraceback:\n{tb}",
            )

        # ── FAZ 5: Cleanup ─────────────────────────────────────────────────
        print("\n[PHASE 5] Running cleanup ...")
        try:
            cleanup_reels_folder()
        except Exception as e:
            print(f"  [WARN] Cleanup error: {e}")

        # ── Cycle Summary ─────────────────────────────────────────────────
        cycle_end      = datetime.datetime.utcnow()
        cycle_duration = (cycle_end - cycle_start).total_seconds() / 3600.0
        next_cycle_dt  = schedule_start + datetime.timedelta(days=NEXT_CYCLE_OFFSET_DAYS)

        print(f"\n{'═'*65}")
        print(f"  CYCLE #{cycle_number} COMPLETE")
        print(f"  Started   : {cycle_start.strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"  Finished  : {cycle_end.strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"  Duration  : {cycle_duration:.1f} hours")
        print(f"  Uploaded  : {success_count} videos")
        print(f"  Failed    : {fail_count} videos")
        print(f"  Next cycle: {next_cycle_dt.strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"{'═'*65}\n")

        send_alert(
            f"Cycle #{cycle_number} Complete",
            f"Cycle completed successfully.\n\n"
            f"Summary:\n"
            f"  ✓ Uploaded : {success_count} videos\n"
            f"  ✗ Failed   : {fail_count} videos\n"
            f"  Duration   : {cycle_duration:.1f} hours\n\n"
            f"Next cycle: {next_cycle_dt.strftime('%Y-%m-%d %H:%M UTC')}",
        )

        # Wait until next cycle, checking every 6 hours
        print(f"[WAITING] Next cycle: {next_cycle_dt.strftime('%Y-%m-%d %H:%M')}")
        while datetime.datetime.utcnow() < next_cycle_dt:
            remaining = (next_cycle_dt - datetime.datetime.utcnow()).total_seconds() / 3600
            print(f"  [WAITING] {remaining:.1f} hours remaining ...")
            time.sleep(6 * 3600)


# ═══════════════════════════════════════════════════════════════
#  FULFILLMENT MODE  –  Gap filler called by fulfillment_engine.py
#  Usage: python sector_processor.py --fulfillment --channel-id N --gap N
# ═══════════════════════════════════════════════════════════════

def fulfillment_run(channel_id: int, gap: int):
    """
    Fills the production gap for ONE specific OwnedChannel.
    Phases run: 2 (Gap-First collect) + 3 (schedule empty slots).
    Phase 4 (upload) is intentionally skipped – uploads happen on their
    scheduled dates via the normal cycle.

    SINGLE-GPU MODE:
      cuda:0 → Qwen (AI scoring + metadata) + Kokoro TTS
                + Whisper transcription + h264_nvenc encoding
    """
    print(f"\n{'='*65}")
    print(f"  FULFILLMENT MODE – Channel #{channel_id}  gap={gap}")
    print(f"  {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*65}\n")

    # ── bg_music klasör iskeletini DB'deki sektörlere göre hazırla ──────────
    ensure_bg_music_folders()

    ch = session.query(OwnedChannels).get(channel_id)
    if not ch:
        print(f"[FULFILL] ERROR: OwnedChannel #{channel_id} not found.")
        return 0

    sector = session.query(Sectors).get(ch.sector_id)
    if not sector:
        print(f"[FULFILL] ERROR: Sector #{ch.sector_id} not found for channel #{channel_id}.")
        return 0

    # Update level before producing (Monday-only guard is inside the function)
    update_channel_levels()

    # ── Phase 2: Collect – newest-first source selection ─────────────────
    already_have = session.query(videos).filter_by(owned_channel_id=channel_id, is_used=False).count()
    total_target = already_have + gap

    print(f"[FULFILL] Channel: {ch.name or 'Unnamed'}  "
          f"Level={ch.level}  Sector='{sector.name}'")
    print(f"[FULFILL] Videos for channel: {already_have} | Need: {total_target} | Gap: {gap}")

    generated = collect_videos_for_sector(sector, total_target, [ch])

    if generated == 0:
        print(f"[FULFILL] No videos generated. Pool may be exhausted. "
              f"Check dashboard warnings.")
        return 0

    # ── Phase 3: Schedule newly produced videos into empty calendar slots ──
    now      = datetime.datetime.utcnow()
    cycle_id = int(now.timestamp())
    scheduled = build_upload_schedule(ch, now, cycle_id)

    print(f"\n[FULFILL] ✓ Channel #{channel_id} ({ch.name}): "
          f"+{generated} produced, {scheduled} scheduled.")
    print(f"[FULFILL] Next scheduled slot fills automatically in the upload cycle.\n")

    return generated


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys as _sys
    import argparse as _argparse

    parser = _argparse.ArgumentParser(
        description="UTube Bot – Sector Processor",
        formatter_class=_argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  (no flags)              Full monthly cycle (collect → schedule → upload)
  --fulfillment           Gap-fill mode: produce & schedule only, no upload

Fulfillment examples:
  python sector_processor.py --fulfillment --channel-id 3 --gap 15
""",
    )
    parser.add_argument("--fulfillment",  action="store_true",
                        help="Run in fulfillment (gap-fill) mode")
    parser.add_argument("--channel-id",   type=int, default=None,
                        help="Target OwnedChannel ID (required for --fulfillment)")
    parser.add_argument("--gap",          type=int, default=0,
                        help="Number of videos to produce (required for --fulfillment)")
    args = parser.parse_args()

    if args.fulfillment:
        if not args.channel_id:
            parser.error("--fulfillment requires --channel-id")
        if args.gap <= 0:
            parser.error("--fulfillment requires --gap > 0")
        _sys.exit(0 if fulfillment_run(args.channel_id, args.gap) >= 0 else 1)
    else:
        main()
