#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# HARDWARE LOCK  --  must run BEFORE any torch / CUDA import!
# Hybrid Mode: AI on cuda:0, ALL rendering forced to CPU (libx264).
# ============================================================================
import os
os.environ["CUDA_DEVICE_ORDER"]   = "PCI_BUS_ID"   # stable GPU enumeration
os.environ["CUDA_VISIBLE_DEVICES"] = "0"            # expose ONLY the primary RTX 3060

"""
legacy_runner.py  -  Hybrid Hardware Video Processing Engine
=============================================================
GPU Assignment (HYBRID MODE — 1x RTX 3060 12 GB):

  cuda:0  [AI Brain — kept loaded between tasks]
    • Whisper transcription   (word-level timestamps)
    • Qwen2.5-14B-Instruct-AWQ  analysis & scripting
    • Kokoro-82M TTS voice generation

  CPU  [Render Engine — libx264 -threads 0, Ryzen 7 parallelism]
    • FFmpeg segment splitting       (libx264 -threads 0)
    • MoviePy final compositing      (libx264 -threads 0)
    • Non-blocking: render thread dispatched async so AI can start next clip

Memory discipline — explicit cache flush after every AI model release:
    - Load Whisper  → Process → del model  → gc.collect() → empty_cache()
    - Load Qwen     → Process → del model  → gc.collect() → empty_cache()
    - Kokoro loaded per-call → del tts     → gc.collect() → empty_cache()
    - No VRAM sleep needed: CPU render runs in parallel thread

OUTPUT SPEC (1080p @ 30fps):
    - Resolution  : 1080 x 1920  (Vertical / 9:16)
    - Frame Rate  : 30 fps
    - Pixel Format: yuv420p
    - Encoder     : libx264 -preset fast -threads 0  (CPU, all cores)
    - Audio       : aac 192k

LAYOUT ZONES (Zero-Overlap Contract):
    Zone 1 (top 25%)   :  y in [0,  H*0.25]  → Infoboxes, Titles
    neutral             :  y in [H*0.25, H*0.70]  → Background + main video
    Zone 2 (bottom 30%):  y in [H*0.70, H]   → Karaoke captions ONLY
"""

# Must be set before torch/transformers import
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import sys
import time
import re
import uuid
import math
import gc
import json
import threading
import traceback
import subprocess
import tempfile
from typing import List, Optional

# -- yt-dlp ------------------------------------------------------------------
try:
    from yt_dlp import YoutubeDL
except ImportError:
    YoutubeDL = None
    print("[WARN] yt-dlp not found. Install: pip install yt-dlp")

# -- MoviePy (optional — no longer used for rendering, kept for legacy utils) --
try:
    from moviepy.editor import (
        VideoFileClip, TextClip, CompositeVideoClip,
        ColorClip, ImageClip, AudioFileClip,
        concatenate_videoclips,
    )
    from moviepy.audio.AudioClip import CompositeAudioClip
    from moviepy.config import change_settings
    change_settings({
        "IMAGEMAGICK_BINARY": r"C:\Program Files\ImageMagick-7.1.2-Q16-HDRI\magick.exe"
    })
    _HAS_MOVIEPY = True
except ImportError:
    _HAS_MOVIEPY = False
    print("[INFO] MoviePy not installed — OK for IG Reels pipeline")

# Pipeline flag — always use FFmpeg (MoviePy fallback removed)
USE_MULTIMODAL_QWEN: bool = True   # Phase 3: enable 6-specialized-prompt pipeline

# -- OpenCV ------------------------------------------------------------------
try:
    import cv2
    import numpy as np
    _HAS_CV2 = True
except ImportError:
    cv2 = None
    np = None
    _HAS_CV2 = False
    print("[WARN] OpenCV not found. pip install opencv-python")

# -- PIL for subscribe-button rendering --------------------------------------
try:
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False
    print("[WARN] Pillow not found - subscribe button disabled. pip install pillow")

# -- Whisper -----------------------------------------------------------------
try:
    import whisper as _whisper_lib
except ImportError:
    _whisper_lib = None
    print("[WARN] whisper not found. pip install openai-whisper")

# -- PyTorch -----------------------------------------------------------------
try:
    import torch
except ImportError:
    torch = None
    print("[WARN] torch not found.")

# -- Transformers (Qwen) -----------------------------------------------------
try:
    from transformers import AutoTokenizer, AutoModelForCausalLM
    from transformers.pipelines import pipeline as hf_pipeline
except ImportError:
    print("[WARN] transformers not found.")
    AutoTokenizer = None
    AutoModelForCausalLM = None
    hf_pipeline = None

# -- Kokoro-ONNX TTS ---------------------------------------------------------
try:
    import kokoro_onnx
    _HAS_KOKORO = True
except ImportError:
    kokoro_onnx = None
    _HAS_KOKORO = False
    print("[WARN] kokoro-onnx not found. pip install kokoro-onnx")

# -- SoundFile ---------------------------------------------------------------
try:
    import soundfile as sf
    _HAS_SF = True
except ImportError:
    sf = None
    _HAS_SF = False
    print("[WARN] soundfile not found. pip install soundfile")

# ============================================================================
# HARDWARE CONSTANTS
# ============================================================================

GPU_AI = "cuda:0"   # All AI inference pinned here

# ============================================================================
# OUTPUT CONSTANTS
# ============================================================================

OUTPUT_FPS     = 60          # 60fps — smooth animations & karaoke on Ryzen 7 CPU render
OUTPUT_W       = 1080
OUTPUT_H       = 1920
OUTPUT_PIX_FMT = "yuv420p"

# ── ZONE ANCHORS  (zero-overlap contract) ───────────────────────────────────
ZONE1_Y_ANCHOR = int(OUTPUT_H * 0.04)   # Top-25% zone: infoboxes, titles
ZONE2_Y_ANCHOR = int(OUTPUT_H * 0.76)   # Bottom-30% zone: karaoke captions

# ── ANIMATION TIMING ────────────────────────────────────────────────────────
SLIDE_FADE_DUR  = 0.6    # seconds — slide-up + fade-in for all Zone-1 elements
SLIDE_OFFSET_PX = 60     # pixels to travel upward during slide-in

# ── INFOBOX MINIMUM DISPLAY ─────────────────────────────────────────────────
INFOBOX_MIN_DUR = 5.0    # seconds minimum on-screen

# ── SUBSCRIBE BUTTON ────────────────────────────────────────────────────────
SUBSCRIBE_BTN_RATIO = 0.37   # 37% of video width ≈ 400 px on 1080p

# ============================================================================
# PATHS & CONFIG
# ============================================================================

SAVE_DIR     = os.path.abspath("artifacts")
DOWNLOAD_DIR = os.path.abspath("downloads")
SHORTS_DIR   = os.path.abspath("shorts")
SEGMENTS_DIR = os.path.abspath("segments")
TTS_DIR      = os.path.abspath("tts_cache")

QWEN_MODEL_NAME   = "Qwen/Qwen2.5-14B-Instruct-AWQ"
CHUNK_DURATION_SEC = 180    # 3-minute max segments
QWEN_WORD_LIMIT    = 1500   # Hard word-cap before every Qwen call


# ============================================================================
# UTILITIES
# ============================================================================

def ensure_dirs():
    for d in [SAVE_DIR, DOWNLOAD_DIR, SHORTS_DIR, SEGMENTS_DIR, TTS_DIR]:
        os.makedirs(d, exist_ok=True)


def seconds_to_timecode(seconds) -> str:
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins:02}:{secs:02}"


def parse_timecode(tc: str) -> float:
    tc    = tc.strip().replace(",", ".")
    parts = tc.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    return 0.0


def is_overlapping(new_start: float, existing_intervals: list,
                   threshold: float = 10) -> bool:
    return any(abs(new_start - s) <= threshold for s in existing_intervals)


def _truncate_transcript(text: str, max_words: int = QWEN_WORD_LIMIT) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    truncated = " ".join(words[:max_words])
    print(f"[TRUNC] Transcript capped at {max_words} words (was {len(words)}).")
    return truncated


def _hex_to_rgb(hex_color: str):
    """Convert #RRGGBB → (R, G, B)."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return (255, 0, 0)
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))



# ============================================================================
# MEMORY MANAGEMENT
# ============================================================================

def cleanup_gpu(device: Optional[str] = None):
    """
    VRAM cleanup — always targets cuda:0.
    gc.collect() called before and after CUDA cache flush.
    """
    gc.collect()
    if torch and torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()
    gc.collect()


# ============================================================================
# 1. DOWNLOAD — Anti-Bot + Verification Gate
# ============================================================================

def download_youtube_video(url: str) -> str:
    """
    Downloads a YouTube video using yt-dlp with web+tv client rotation,
    Node.js JS-runtime pinning, and EJS remote-component n-challenge solving.

    Execution is strictly sequential:
      subprocess.run() blocks until yt-dlp + FFmpeg fully finish, then a
      mandatory 5-second cooldown lets Windows release all NTFS file locks
      before the verification gate starts polling.

    Verification gate (60 s):
      - Waits for all .part / .temp files to disappear.
      - Requires the final .mp4 to be > 1 MB AND size-stable for 3 consecutive
        seconds before accepting it.
      - PermissionError (WinError 32) on getsize is caught; waits 2 s and retries.

    Error policy:
      - yt-dlp non-zero exit  -> delete partial file (corrupted) and raise.
      - Verification timeout  -> rename file to .retry for manual inspection; raise.
    """
    ensure_dirs()

    # Deterministic filename from YouTube video ID
    match  = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]+)", url)
    vid_id = match.group(1) if match else str(int(time.time()))
    video_path = os.path.join(os.path.abspath("downloads"), f"{vid_id}.mp4")

    # NOTE: yt-dlp self-update (pip install -U yt-dlp) and cache clear
    # (--rm-cache-dir) are NOT run here anymore.
    # — Self-update: run once at startup in weekly_cycle.py, not per video.
    # — Cache clear: only done inside the retry loop in sector_processor.py
    #   AFTER a failure, not before clean downloads (it was wasting 2-5s per video).

    cmd = [
        sys.executable, "-m", "yt_dlp",
        # android_vr bypasses n-challenge; web supports Chrome cookies.
        "--extractor-args", "youtube:player_client=android_vr,web,tv",
        # Node.js JS solver for n-challenge (confirmed working manually).
        "--js-runtimes", r"node:C:\Program Files\nodejs\node.exe",
        # Use GitHub-hosted EJS scripts to solve the n-challenge remotely.
        "--remote-components", "ejs:github",
        "-f", "bestvideo[height<=1080][fps<=60]+bestaudio/best[ext=m4a]/best",
        "--merge-output-format", "mp4",
        "--cookies", "cookies.txt",
        "--no-check-certificate",
        "-o", video_path,
        url,
    ]

    print(f"[DL] Starting download: {url}")
    print(f"[DL] Output path: {video_path}")

    # subprocess.run() blocks until yt-dlp AND the FFmpeg mux step fully finish.
    try:
        subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=600
        )
    except subprocess.CalledProcessError as e:
        # Hard failure: yt-dlp returned non-zero -- delete corrupted partial file.
        print(f"[DL ERROR] yt-dlp exited {e.returncode}")
        print(f"[DL STDOUT] {e.stdout[-2000:]}")
        print(f"[DL STDERR] {e.stderr[-2000:]}")
        if os.path.exists(video_path):
            try:
                os.remove(video_path)
                print(f"[DL] Partial file deleted after non-zero exit: {video_path}")
            except Exception:
                pass
        raise RuntimeError(f"Download failed for {url}: exit code {e.returncode}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Download timed out (600s) for {url}")

    # ── Mandatory 5-second cooldown ───────────────────────────────────────────
    # Even after subprocess.run() returns, Windows may still hold NTFS file
    # locks (Explorer thumbnail, AV scanner, indexer).  This gap lets them go.
    print("[DL] Process exited. Waiting 5 s for Windows file-lock release ...")
    time.sleep(5)

    # ── 60-second size-stable verification gate ───────────────────────────────
    # Conditions for acceptance (all three must hold):
    #   1. No .part / .temp sibling files in the download dir.
    #   2. Final .mp4 exists and is > 1 MB.
    #   3. File size unchanged for 3 consecutive 1-second polls.
    # WinError 32 guard: PermissionError on getsize → wait 2 s and retry.
    print("[DL] Verifying file on disk (size-stable gate, 60 s) ...")
    dl_dir        = os.path.dirname(video_path)
    deadline      = time.time() + 60
    prev_size     = -1
    stable_count  = 0
    STABLE_NEEDED = 3    # consecutive identical-size readings required

    while time.time() < deadline:
        # ── Check for temp files (FFmpeg still renaming) ──────────────────────
        try:
            tmp_files = [
                f for f in os.listdir(dl_dir)
                if f.startswith(vid_id) and (f.endswith(".part") or f.endswith(".temp"))
            ]
        except PermissionError:
            time.sleep(2)
            continue

        if tmp_files:
            print(f"[DL]   Temp files still present: {tmp_files} — waiting ...")
            stable_count = 0
            time.sleep(1)
            continue

        if not os.path.exists(video_path):
            time.sleep(1)
            continue

        # ── WinError 32 guard ─────────────────────────────────────────────────
        try:
            cur_size = os.path.getsize(video_path)
        except PermissionError:
            print("[DL]   File locked (WinError 32) — retrying in 2 s ...")
            stable_count = 0
            time.sleep(2)
            continue

        if cur_size < 1_000_000:
            time.sleep(1)
            continue

        if cur_size == prev_size:
            stable_count += 1
        else:
            stable_count = 0

        prev_size = cur_size

        if stable_count >= STABLE_NEEDED:
            size_mb = cur_size / (1024 * 1024)
            print(f"[DL] ✓ Verified (size-stable): {video_path}  ({size_mb:.1f} MB)")
            # Fix WinError 32: give Windows 2 extra seconds to release all
            # NTFS handles (Explorer thumbnail, AV scanner) before the caller
            # tries to rename/move the file.
            time.sleep(2)
            return video_path

        time.sleep(1)

    # Verification timed out — rename to .retry for manual inspection.
    # Do NOT delete: the file may be fine but FFmpeg took longer than 60 s.
    retry_path = video_path + ".retry"
    if os.path.exists(video_path):
        try:
            os.rename(video_path, retry_path)
            print(f"[DL] Verification timeout — renamed to: {retry_path}")
        except Exception:
            pass

    raise RuntimeError(
        f"[DL] Verification FAILED after 60 s: {video_path} "
        f"not stable or < 1 MB. Renamed to {retry_path} for inspection."
    )


# ============================================================================
# 2a. AI: WHISPER — cuda:0  (word-level timestamps)
# ============================================================================

def generate_segment_transcript(video_path: str) -> str:
    """
    Segment-level Whisper transcription on cuda:0.
    Returns timecoded transcript string.
    Fully unloads model from VRAM on completion.
    """
    cleanup_gpu(GPU_AI)
    print(f"[TRANSCRIPT] Whisper ({GPU_AI}): {os.path.basename(video_path)}")
    model = None
    try:
        model = _whisper_lib.load_model("small", device=GPU_AI)
        result = model.transcribe(
            video_path, language="en", fp16=True, beam_size=1, best_of=1,
        )
        formatted = ""
        for seg in result.get("segments", []):
            start = seconds_to_timecode(seg["start"])
            end   = seconds_to_timecode(seg["end"])
            text  = seg["text"].strip()
            formatted += f"[{start} - {end}] {text}\n"
        return formatted
    except Exception as e:
        print(f"[ERROR] Transcript error: {e}")
        return ""
    finally:
        if model: del model
        gc.collect()
        if torch and torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()


def transcribe_subclip_for_karaoke(audio_path: str) -> List[dict]:
    """
    Word-level Whisper transcription for karaoke subtitles on cuda:0.
    Returns list of {start, end, text} dicts.
    """
    cleanup_gpu(GPU_AI)
    model = None
    try:
        model = _whisper_lib.load_model("small", device=GPU_AI)
        result = model.transcribe(
            audio_path, word_timestamps=True, language="en", fp16=True, beam_size=1
        )
        word_segments = []
        for seg in result.get("segments", []):
            for word in seg.get("words", []):
                if isinstance(word, dict):
                    word_segments.append({
                        "start": word.get("start", 0),
                        "end":   word.get("end",   0),
                        "text":  word.get("word",  "").strip(),
                    })
        return word_segments
    except Exception as e:
        print(f"[KARAOKE] Whisper error: {e}")
        return []
    finally:
        if model: del model
        gc.collect()
        if torch and torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()


# ============================================================================
# 2b. AI: QWEN2.5-14B-AWQ — cuda:0
# ============================================================================

def send_prompt_to_qwen(messages: List[dict], max_tokens: int = 2000) -> str:
    """
    Loads Qwen2.5-14B-AWQ exclusively on cuda:0, generates a response,
    then DELETES the model and calls empty_cache() immediately.
    """
    cleanup_gpu(GPU_AI)
    print("[QWEN] -- Loading Qwen2.5-14B-AWQ onto cuda:0 --")
    pipe = model = tokenizer = None
    try:
        tokenizer = AutoTokenizer.from_pretrained(QWEN_MODEL_NAME, use_fast=False)
        if tokenizer.pad_token is None:
            tokenizer.pad_token    = tokenizer.eos_token
            tokenizer.pad_token_id = tokenizer.eos_token_id

        model = AutoModelForCausalLM.from_pretrained(
            QWEN_MODEL_NAME,
            trust_remote_code=True,
            device_map={"": 0},
            torch_dtype=torch.float16,
            offload_buffers=True,
        )
        pipe = hf_pipeline(
            "text-generation",
            model=model,
            tokenizer=tokenizer,
            device_map={"": 0},
        )

        prompt_text     = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        input_ids       = tokenizer(prompt_text, return_tensors="pt")["input_ids"]
        input_token_len = input_ids.shape[-1]
        print(
            f"[QWEN] Prompt tokens: {input_token_len:,} | "
            f"max_new_tokens: {max_tokens} | "
            f"budget: {12288 - input_token_len:,} remaining"
        )

        t0 = time.time()
        with torch.no_grad():
            outputs = pipe(
                prompt_text,
                max_new_tokens=max_tokens,
                do_sample=True,
                temperature=0.6,
                top_p=0.9,
                repetition_penalty=1.1,
                return_full_text=False,
            )
        result  = outputs[0]["generated_text"].strip()
        elapsed = time.time() - t0
        out_tok = len(tokenizer(result)["input_ids"])
        print(
            f"[QWEN] Done | tokens={out_tok} | "
            f"elapsed={elapsed:.1f}s | speed={out_tok/max(elapsed,0.1):.1f} tok/s"
        )
        return result

    except Exception as e:
        print(f"[QWEN] ERROR: {e}")
        return ""
    finally:
        if pipe:      del pipe
        if model:     del model
        if tokenizer: del tokenizer
        gc.collect()
        if torch and torch.cuda.is_available():
            # Explicit cache flush immediately after Qwen releases VRAM
            torch.cuda.empty_cache()
        gc.collect()
        print("[QWEN] VRAM released.")


# ── Sector accent colors (for dynamic banners & progress bar) ────────────────
SECTOR_COLORS: dict = {
    "tech":       "#1E90FF",   # Dodger Blue
    "technology": "#1E90FF",
    "business":   "#00C853",   # Green
    "finance":    "#00C853",
    "gaming":     "#FF6D00",   # Deep Orange
    "sports":     "#FF6D00",
    "health":     "#00BFA5",   # Teal
    "science":    "#AA00FF",   # Purple
    "politics":   "#D50000",   # Red
    "news":       "#FF6F00",   # Amber
    "entertainment": "#C51162",  # Pink
    "default":    "#39FF14",   # Neon Green
}


def _sector_accent(sector_name: str, fallback: str = "#39FF14") -> str:
    """Returns the accent hex color for a given sector name."""
    key = (sector_name or "").lower().strip()
    for k, v in SECTOR_COLORS.items():
        if k in key:
            return v
    return SECTOR_COLORS.get("default", fallback)


# ── SOURCE ATTRIBUTION CONFIG ────────────────────────────────────────────────
ATTRIBUTION_CHANNEL = "Prompt Engineering Lab"   # ← edit to your channel name


# ---------------------------------------------------------------------------- #
# 4-PART JSON SCRIPT SYSTEM  (Qwen-14B)                                        #
# ---------------------------------------------------------------------------- #

# NOTE: sector_name is injected at call-time into the user message so Qwen
# assumes a domain-specific expert persona for every clip.
_SCRIPT_SYSTEM = """\
You are a world-class expert analyst and YouTube Shorts scriptwriter.
Your ONLY output is a single, raw, valid JSON object — NO markdown, NO prose,
NO explanation, NO triple backticks, NO text before or after the JSON.

Persona rule: adopt the perspective of a high-level field expert for the sector
provided in the user message. Examples:
  • Sector = Intelligence / CIA    → speak as a senior intelligence analyst.
  • Sector = Finance / Economics   → speak as a hedge-fund strategist.
  • Sector = Health / Medicine     → speak as a board-certified specialist.
  • Sector = Tech / AI             → speak as a principal ML researcher.
  • Sector = Gaming / Esports      → speak as a pro-player analyst.
  (Infer the best expert role when the sector is ambiguous.)

STRICTLY FORBIDDEN phrases (instant failure if used anywhere in the JSON):
  • "Check this out"
  • "Notable moment"
  • "Worth your attention"
  • "Interesting"
  • "This is about"
  • "In this clip"

Required JSON keys (ALL SEVEN MANDATORY — never omit, never add extras):

{
  "hook_text":         "<3-5 WORDS ALL-CAPS. MUST contain at least ONE power word from: DANGEROUS, SECRET, EXPOSED, BANNED, SHOCKING, ILLEGAL, TRICK, REALITY, HIDDEN, LEAKED, FORBIDDEN, PROOF, WARNING, DARK SIDE>",
  "technical_insight": "<MAX 10 WORDS. Format: '[Subject] → [Simple outcome]'. Plain conversational language only. NO academic or medical phrasing. Good example: 'More eye contact = more trust perceived'. Bad example: 'The heightened emotional resonance results from increased pupil dilation'>",
  "judgment":          "<MUST start with 'Honestly,' or 'In reality,'. 1-2 sentences. A CONTROVERSIAL, thought-provoking opinion that will make viewers stop scrolling and argue in the comments. Do NOT hedge or soften.>",
  "outro_question":    "<A sharp, debate-triggering question addressed directly to the viewer. End the string with ' — Subscribe for more!' >",
  "title":             "<Clickbait title using a power word. Max 80 chars.>",
  "infobox_1":         "<Fact Check: [specific claim from the transcript] is TRUE / FALSE / MISLEADING!>",
  "infobox_2":         "<Did You Know? [one surprising, specific, verifiable fact directly related to the clip topic]>"
}

FORMAT RULES (critical for valid JSON):
  • All values are plain strings — no nested objects or arrays.
  • Escape any double-quote inside a value with a backslash: \".
  • No trailing commas.
  • Output the JSON object and NOTHING else."""


def _qwen_call_adapter(prompt: str) -> str:
    """Adapter: single-string prompt → OpenAI messages format expected by send_prompt_to_qwen."""
    messages = [
        {"role": "user", "content": prompt}
    ]
    return send_prompt_to_qwen(messages, max_tokens=1000)


def _generate_script_multimodal(video_path, clip_start, clip_end, audio_path, 
                                 word_segments, niche, source_channel):
    """
    New multi-prompt pipeline: builds rich context bundle, runs 5 specialized 
    Qwen prompts, returns the same dict structure as the legacy function.
    """
    import multimodal_context
    import qwen_specialized
    
    t0 = time.time()
    print(f"[QWEN MULTIMODAL] Building context bundle...")
    
    bundle = multimodal_context.build_context_bundle(
        video_path=video_path,
        clip_start=clip_start,
        clip_end=clip_end,
        audio_path=audio_path,
        word_segments=word_segments,
    )
    
    print(f"[QWEN MULTIMODAL] Context built in {time.time()-t0:.1f}s — "
          f"{len(bundle.get('enriched_transcript', []))} words, "
          f"{len(bundle.get('scene_changes', []))} scene cuts, "
          f"peak={bundle.get('audio_peak_time', 0):.1f}s")
    
    t1 = time.time()
    outputs = qwen_specialized.generate_all_outputs(
        context_bundle=bundle,
        niche=niche,
        source_channel=source_channel,
        qwen_call_fn=_qwen_call_adapter,
    )
    print(f"[QWEN MULTIMODAL] 5 prompts completed in {time.time()-t1:.1f}s")
    
    return {
        "hook_text":          outputs.get("hook_text", ""),
        "technical_insight":  outputs.get("technical_insight", ""),
        "infobox_1":          outputs.get("infobox_1", ""),
        "infobox_2":          outputs.get("infobox_2", ""),
        "judgment":           outputs.get("judgment", ""),
        "outro_question":     outputs.get("cta_question", ""),
        "voice_id":           outputs.get("voice_recommendation", "af_sky"),
        "title":              outputs.get("title", "New Short"),
        "caption":            outputs.get("caption", ""),
        "hashtags":           outputs.get("hashtags", []),
    }


def generate_script_with_qwen(transcript: str, sector_name: str,
                               persona: str = "", video_path=None, clip_start=None, 
                               clip_end=None, audio_path=None, word_segments=None,
                               source_channel_handle=None, **kwargs) -> dict:
    """
    Calls Qwen to generate the 4-part script JSON for a clip.

    Returns dict with keys:
        hook_text, technical_insight, judgment, outro_question,
        title, infobox_1, infobox_2

    Fallback: returns safe defaults so render pipeline never crashes.
    """
    print(f"[QWEN ROUTER] USE_MULTIMODAL_QWEN={USE_MULTIMODAL_QWEN}, video_path={'YES' if video_path else 'NO'}, word_segments={len(word_segments) if word_segments else 0}")
    
    # Phase 3 router: multimodal multi-prompt path
    if USE_MULTIMODAL_QWEN and video_path and word_segments:
        try:
            return _generate_script_multimodal(
                video_path=video_path,
                clip_start=clip_start,
                clip_end=clip_end,
                audio_path=audio_path,
                word_segments=word_segments,
                niche=sector_name,
                source_channel=source_channel_handle,
            )
        except Exception as e:
            print(f"[QWEN MULTIMODAL] FAILED with exception: {type(e).__name__}: {e}")
            import traceback; traceback.print_exc()
            # fall through to original implementation

    if not transcript.strip():
        return {}

    safe_transcript = _truncate_transcript(transcript)

    # Persona injection: prepend the channel's expert directive to the system prompt
    persona_directive = (
        f"\n\nMANDATORY PERSONA: {persona}\n"
        f"You MUST speak exclusively in this voice throughout all JSON values."
        if persona else ""
    )
    system_with_persona = _SCRIPT_SYSTEM + persona_directive

    persona_line = (
        f"ACTIVE PERSONA: {persona}\n"
        if persona else
        f"EXPERT PERSONA: Adopt the most relevant world-class expert role for '{sector_name}'.\n"
    )
    user_msg = (
        f"SECTOR: {sector_name}\n"
        f"{persona_line}\n"
        f"TRANSCRIPT:\n{safe_transcript}\n\n"
        f"OUTPUT ONLY the raw JSON object. "
        f"Every string value must be specific, expert-level, and free of generic filler."
    )
    messages = [
        {"role": "system", "content": system_with_persona},
        {"role": "user",   "content": user_msg},
    ]

    _FALLBACK_META = {
        "hook_text":         "CHECK THIS OUT",
        "technical_insight": "This clip contains a notable technical moment.",
        "judgment":          "Honestly, this is worth your attention.",
        "outro_question":    "What do you think? Subscribe for more!",
        "title":             f"{sector_name} Clip",
        "infobox_1":         "",
        "infobox_2":         "",
    }

    def _has_chinese(text):
        return any('\u4e00' <= c <= '\u9fff' for c in (text or ''))

    for attempt in range(2):
        raw = send_prompt_to_qwen(messages, max_tokens=500)

        # Stage 1 – isolate first JSON block
        _json_match = re.search(r'(\{[\s\S]*\})', raw)
        if not _json_match:
            if attempt == 0:
                print(f"[QWEN-SCRIPT] No JSON block found, retrying...")
                continue
            print(f"[QWEN-SCRIPT] No JSON block found — fallback. Raw[:200]: {raw[:200]}")
            return _FALLBACK_META

        # Stage 2 – strip control characters (ASCII 0-8, 11-12, 14-31)
        json_str = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', _json_match.group(1))

        try:
            meta    = json.loads(json_str)
            hook = meta.get("hook_text", "")
            judgment = meta.get("judgment", "")
            outro_question = meta.get("outro_question", "")
            print(f"[QWEN] Chinese check: hook={_has_chinese(hook)}, judgment={_has_chinese(judgment)}, cta={_has_chinese(outro_question)}")
            if _has_chinese(hook) or _has_chinese(judgment) or _has_chinese(outro_question):
                print(f"[QWEN] Chinese detected in output, retrying...")
                if attempt == 0:
                    continue
            
            req     = {"hook_text", "technical_insight", "judgment", "outro_question"}
            missing = req - meta.keys()
            if missing:
                print(f"[QWEN-SCRIPT] Missing keys: {missing} — filling defaults.")
                defaults = {
                    "hook_text":         "WATCH THIS NOW",
                    "technical_insight": "This clip contains a notable technical moment.",
                    "judgment":          "Honestly, this is worth your attention.",
                    "outro_question":    "What did you think? Subscribe for more!",
                }
                for k in missing:
                    meta[k] = defaults[k]
            meta.setdefault("title",     f"{sector_name} Clip")
            meta.setdefault("infobox_1", "")
            meta.setdefault("infobox_2", "")
            print(f"[QWEN-SCRIPT] ✓ hook='{meta['hook_text']}' | judgment='{meta['judgment'][:40]}...'")
            return meta
        except json.JSONDecodeError as e:
            if attempt == 0:
                print(f"[QWEN-SCRIPT] JSONDecodeError @ pos {e.pos}, retrying...")
                continue
            ctx_start = max(0, e.pos - 30)
            ctx_end   = min(len(json_str), e.pos + 30)
            print(
                f"[QWEN-SCRIPT] JSONDecodeError @ pos {e.pos}: {e.msg}\n"
                f"  context: ...{repr(json_str[ctx_start:ctx_end])}...\n"
                f"  → using fallback."
            )
            return _FALLBACK_META
            
    return _FALLBACK_META


# Keep legacy alias so standalone main() still works without changes
def analyze_clip_with_qwen(transcript: str, sector_name: str) -> dict:
    """Backwards-compatible shim → delegates to generate_script_with_qwen."""
    return generate_script_with_qwen(transcript, sector_name)


# ============================================================================
# 2c. TTS: KOKORO-ONNX — cuda:0  (standard Kokoro class, NOT KPipeline)
# ============================================================================

def _validate_voices_file(voices_path: str) -> str:
    """
    Validates the voices file and returns the path to a usable binary file.

    Strategy:
      1. Show first 100 chars of the file for diagnostics.
      2. If the file is a valid JSON or plaintext (not binary), it is the OLD
         format — the actual voices data we need is 'voices-v1.0.bin'.
      3. If 'voices-v1.0.bin' exists locally, use it.
      4. Otherwise download it from the official GitHub release.

    Returns the resolved path to a binary voices file, or raises RuntimeError.
    """
    import urllib.request

    VOICES_BIN      = "voices-v1.0.bin"
    VOICES_BIN_URL  = (
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.0/voices-v1.0.bin"
    )

    # ── 1. Diagnostic header dump ─────────────────────────────────────────────
    if os.path.exists(voices_path):
        try:
            with open(voices_path, "rb") as f:
                raw_header = f.read(100)
            try:
                header_str = raw_header.decode("utf-8", errors="replace")
            except Exception:
                header_str = repr(raw_header)
            print(f"[DEBUG] File Header ({voices_path}): {header_str[:100]}")
        except Exception as e:
            print(f"[DEBUG] Could not read header of {voices_path}: {e}")
    else:
        print(f"[DEBUG] {voices_path} does not exist.")

    # ── 2. Check if it's a valid binary (not JSON / plaintext) ───────────────
    def _is_binary_voices(path: str) -> bool:
        if not os.path.exists(path) or os.path.getsize(path) < 1024:
            return False
        try:
            with open(path, "rb") as f:
                chunk = f.read(512)
            # JSON / plaintext files are all printable ASCII; binary has high bytes
            high_byte_ratio = sum(b > 127 for b in chunk) / max(len(chunk), 1)
            return high_byte_ratio > 0.05   # >5% high bytes = binary
        except Exception:
            return False

    # ── 3. If voices-v1.0.bin already exists locally and is valid, use it ────
    if _is_binary_voices(VOICES_BIN):
        print(f"[TTS] Using local binary voices file: {VOICES_BIN}")
        return VOICES_BIN

    # ── 4. If the supplied path is already a valid binary, use it directly ────
    if voices_path != VOICES_BIN and _is_binary_voices(voices_path):
        print(f"[TTS] Supplied voices file is valid binary: {voices_path}")
        return voices_path

    # ── 5. Download voices-v1.0.bin from GitHub ───────────────────────────────
    print(f"[TTS] voices file is invalid or missing. Downloading {VOICES_BIN} ...")
    print(f"[TTS] URL: {VOICES_BIN_URL}")
    try:
        urllib.request.urlretrieve(VOICES_BIN_URL, VOICES_BIN)
        if _is_binary_voices(VOICES_BIN):
            size_mb = os.path.getsize(VOICES_BIN) / (1024 * 1024)
            print(f"[TTS] Downloaded {VOICES_BIN}  ({size_mb:.1f} MB)")
            return VOICES_BIN
        else:
            raise RuntimeError(f"Downloaded {VOICES_BIN} still not a valid binary.")
    except Exception as e:
        raise RuntimeError(f"Failed to download voices file: {e}") from e


def generate_kokoro_tts(text: str, voice_id: str, out_path: str) -> bool:
    """
    Generates TTS audio using kokoro_onnx.

    Voice file resolution (auto-repair):
      voices.json is the OLD / invalid format. This function detects it
      automatically, downloads voices-v1.0.bin from GitHub, and uses it.

    API compatibility:
      Tries v1.0 API (.create()) first, then falls back to the legacy
      iterator API for older kokoro_onnx installs.

    Fallback voices (tried in order if primary fails):
      voice_id -> af_sky -> af_sarah -> af -> am_adam

    If ALL attempts fail the function returns False — the caller's silent-
    outro fallback keeps the render thread running (no crash, YPP compliant).
    """
    if not _HAS_KOKORO:
        print("[TTS] kokoro-onnx not installed — skipping.")
        return False
    if not _HAS_SF:
        print("[TTS] soundfile not installed — cannot save .wav.")
        return False
    if not text or not text.strip():
        print("[TTS] Empty text — skipping.")
        return False

    cleanup_gpu(GPU_AI)
    print(f"[TTS] Kokoro: voice={voice_id}, text={text[:60]}...")

    import numpy as _np
    try:
        import onnxruntime as _ort
        _HAS_ORT = True
    except ImportError:
        _ort = None
        _HAS_ORT = False

    try:
        os.makedirs(os.path.dirname(out_path) if os.path.dirname(out_path) else ".", exist_ok=True)

        if not hasattr(kokoro_onnx, "Kokoro"):
            print("[TTS] kokoro_onnx.Kokoro not found — update pip package.")
            return False

        # ── Resolve / auto-repair the voices file ─────────────────────────────
        try:
            voices_file = _validate_voices_file("voices.json")
        except RuntimeError as e:
            print(f"[TTS] Voices file resolution failed: {e}")
            return False

        # Prefer the v1.0 model filename if it exists alongside the legacy one
        model_file = "voices-v1.0.bin"   # will be overridden below
        if os.path.exists("kokoro-v1.0.onnx"):
            model_file = "kokoro-v1.0.onnx"
        elif os.path.exists("model.onnx"):
            model_file = "model.onnx"
        else:
            print("[TTS] No ONNX model file found (kokoro-v1.0.onnx or model.onnx).")
            return False

        print(f"[TTS] Model: {model_file}  |  Voices: {voices_file}")

        # ── Fallback voice candidates ─────────────────────────────────────────
        FALLBACK_VOICES = [voice_id, "af_sky", "af_sarah", "af", "am_adam"]
        seen, voice_candidates = set(), []
        for v in FALLBACK_VOICES:
            if v not in seen:
                seen.add(v)
                voice_candidates.append(v)

        # ── ONNX Runtime input tensor monkey-patch ───────────────────────────
        # Root cause: kokoro-onnx passes token/style/speed tensors as int32,
        # but kokoro-v1.0.onnx expects tensor(float) for those inputs.
        # We intercept InferenceSession.run and cast any integer feed to float32
        # before the call reaches the model — restoring the original in finally.
        _original_ort_run = None
        if _HAS_ORT:
            _original_ort_run = _ort.InferenceSession.run
            def _patched_ort_run(self, output_names, input_feed, run_options=None):
                new_feed = {
                    k: (v.astype(_np.float32)
                        if isinstance(v, _np.ndarray) and _np.issubdtype(v.dtype, _np.integer)
                        else v)
                    for k, v in input_feed.items()
                }
                return _original_ort_run(self, output_names, new_feed, run_options)
            _ort.InferenceSession.run = _patched_ort_run
            print("[DEBUG] ORT InferenceSession.run patched: int inputs will be cast to float32.")

        # ── v1.0 .create() API — only supported path in kokoro-onnx >= 1.0 ──────
        # The legacy iterator API (`tts(text, ...)`) was removed in v1.0;
        # 'Kokoro' object is no longer callable, only .create() works.
        for candidate in voice_candidates:
            try:
                tts = kokoro_onnx.Kokoro(model_file, voices_file)
                samples, sample_rate = tts.create(
                    text, voice=candidate, speed=1.0, lang="en-us"
                )
                # Cast to float32 (ONNX model expects tensor(float), not int32)
                samples = _np.array(samples, dtype=_np.float32)
                # Flatten (1, N) → (N,) — soundfile requires a 1-D array
                samples = samples.flatten()
                print(f"[DEBUG] Final samples shape: {samples.shape}  dtype={samples.dtype}  sample_rate={sample_rate}")

                # ── PCM 16-bit normalization (most compatible WAV format) ───────────
                # Raw ONNX float32 output can have OOB amplitudes that make
                # soundfile write a technically-valid but "unrecognised" WAV
                # header from the perspective of FFmpeg / MoviePy.
                peak = _np.abs(samples).max()
                if peak > 0:
                    samples = samples / peak          # normalize to [-1.0, 1.0]
                samples = _np.clip(samples, -1.0, 1.0)
                samples = (samples * 32767).astype(_np.int16)   # int16 PCM
                print(f"[DEBUG] PCM16 samples: shape={samples.shape}  dtype={samples.dtype}")

                # Remove stale cache file to avoid WinError 32 / format conflicts
                if os.path.exists(out_path):
                    try:
                        os.remove(out_path)
                    except Exception as _rm_err:
                        print(f"[TTS] Warning: could not remove stale file {out_path}: {_rm_err}")

                sf.write(out_path, samples, int(sample_rate), subtype="PCM_16")

                # 1-second handshake: let Windows finish flushing the WAV header
                # before MoviePy / FFmpeg open the file for composition.
                time.sleep(1)

                print(f"[DEBUG] TTS Engine successfully wrote PCM16 WAV. voice={candidate}")
                print(f"[TTS] Saved: {out_path}")
                return True
            except Exception as e:
                print(f"[TTS] v1.0 API failed for voice '{candidate}': {e}")

        print("[TTS] All voice candidates exhausted.")
        return False

    except FileNotFoundError as e:
        print(
            f"[TTS] Model file not found.\n"
            f"      Error: {e}\n"
            f"      Fix: Place 'kokoro-v1.0.onnx' (or 'model.onnx') in the working dir."
        )
        return False
    except Exception as e:
        print(f"[TTS] Unexpected error: {e}")
        return False
    finally:
        # ── Restore original ORT run method ──────────────────────────────────
        if _HAS_ORT and _original_ort_run is not None:
            _ort.InferenceSession.run = _original_ort_run
        gc.collect()
        if torch and torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()


# ============================================================================
# 3. VIDEO SPLITTING — CPU libx264 -threads 0
# ============================================================================

def split_video_into_segments(video_path: str) -> List[str]:
    """
    Split video into CHUNK_DURATION_SEC segments using FFmpeg stream copy.
    NO re-encoding — segments produced in seconds, not minutes.
    Returns list of segment file paths.
    """
    ensure_dirs()
    if not os.path.exists(video_path) or os.path.getsize(video_path) < 1_000_000:
        raise Exception("Split failed: file missing or < 1 MB.")

    video_path   = os.path.abspath(video_path)
    segments_dir = os.path.abspath("segments")
    os.makedirs(segments_dir, exist_ok=True)

    # Probe duration via ffprobe
    try:
        probe_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", video_path
        ]
        probe_out = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
        duration  = float(json.loads(probe_out.stdout)["format"]["duration"])
    except Exception:
        # Fallback: MoviePy reader
        from moviepy.editor import VideoFileClip
        tmp_clip = VideoFileClip(video_path)
        duration = tmp_clip.duration
        tmp_clip.close()

    num_chunks = math.ceil(duration / CHUNK_DURATION_SEC)
    base_name  = os.path.splitext(os.path.basename(video_path))[0]
    print(f"[SPLIT] {duration:.1f}s → {num_chunks} segments (FFmpeg stream-copy)")

    segments = []
    for i in range(num_chunks):
        start          = i * CHUNK_DURATION_SEC
        chunk_filename = os.path.join(segments_dir, f"{base_name}_part{i+1}.mp4")

        if os.path.exists(chunk_filename) and os.path.getsize(chunk_filename) > 1024:
            print(f"[SKIP] Segment exists: {chunk_filename}")
            segments.append(chunk_filename)
            continue

        print(f"[SPLIT] Segment {i+1}/{num_chunks} ...")
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-t", str(CHUNK_DURATION_SEC),
            "-i", video_path,
            "-c", "copy",              # NO re-encoding — pure stream copy
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            chunk_filename,
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                print(f"[SPLIT] ✗ Segment {i+1} failed: {r.stderr[-500:]}")
                continue
            if os.path.exists(chunk_filename) and os.path.getsize(chunk_filename) > 1024:
                print(f"[SPLIT] ✓ Segment {i+1} saved.")
                segments.append(chunk_filename)
            else:
                print(f"[SPLIT] ✗ Segment {i+1} empty after stream copy.")
        except subprocess.TimeoutExpired:
            print(f"[SPLIT] Timeout on segment {i+1}")
            continue

    return segments


# ============================================================================
# 4. BLUR — OpenCV CPU (CUDA optional)
# ============================================================================

def blur_clip(clip, ksize: int = 121):
    """Applies Gaussian blur via OpenCV (CUDA if available, else CPU)."""
    if cv2 is None:
        return clip
    _has_cuda = hasattr(cv2, "cuda") and cv2.cuda.getCudaEnabledDeviceCount() > 0
    if _has_cuda:
        gauss = cv2.cuda.createGaussianFilter(cv2.CV_8UC3, cv2.CV_8UC3, (ksize, ksize), 0)
        def _cuda_blur(frame):
            gpu_frame = cv2.cuda_GpuMat()
            gpu_frame.upload(frame)
            return gauss.apply(gpu_frame).download()
        return clip.fl_image(_cuda_blur)
    else:
        def _cpu_blur(frame):
            return cv2.GaussianBlur(frame, (ksize, ksize), 0)
        return clip.fl_image(_cpu_blur)


# ============================================================================
# 4b. BACKGROUND MUSIC — sector-based loop with smart audio ducking
# ============================================================================

import random

BG_MUSIC_DIR = os.path.abspath("bg_music")

# Global volume levels
BG_MUSIC_GLOBAL_VOL = 0.15   # 15% during main video (audible but not dominant)
BG_MUSIC_DUCK_VOL   = 0.05   # 5% during outro TTS only (ducked)


# ── SFX TRANSITIONS (played once at main→outro boundary) ────────────────────
SFX_DIR = os.path.abspath("sfx_transitions")


def pick_sfx_transition(sfx_set: str = "default") -> Optional[str]:
    """
    Returns a random .wav/.mp3 from sfx_transitions/{sfx_set}/.
    Falls back to sfx_transitions/ root if the subfolder is empty or missing.
    Returns None if no tracks exist anywhere.
    """
    slug    = (sfx_set or "default").strip().lower()
    # Candidates in priority order: set subfolder → root
    search_dirs = [
        os.path.join(SFX_DIR, slug),
        SFX_DIR,
    ]
    for folder in search_dirs:
        if not os.path.isdir(folder):
            continue
        tracks = [
            os.path.join(folder, f)
            for f in os.listdir(folder)
            if f.lower().endswith((".wav", ".mp3"))
        ]
        if tracks:
            chosen = random.choice(tracks)
            print(f"[SFX] set='{slug}' → {os.path.basename(chosen)}")
            return chosen
    print(f"[SFX] No transition SFX found (set='{slug}')")
    return None


# ── TEXT CLIP HELPER — font with Impact fallback ─────────────────────────────

def _make_text_clip(text: str, fontsize: int, font: str, color: str, **kwargs):
    """Attempts TextClip with requested font; retries with Impact on failure."""
    try:
        return TextClip(text, fontsize=fontsize, font=font, color=color, **kwargs)
    except Exception:
        return TextClip(text, fontsize=fontsize, font="Impact", color=color, **kwargs)


def pick_bg_music(sector_name: str) -> Optional[str]:
    """
    Returns a random .mp3/.wav track path from bg_music/{sector_name}/.
    Falls back to bg_music/default/ if the sector folder is empty/missing.
    Returns None if no music is found anywhere.

    Folder-name normalisation rule (must match ensure_bg_music_folders):
      re.sub(r'[^a-z0-9]+', '_', sector.lower()).strip('_')
    Examples:
      'Education (Riskli)' → 'education_riskli'
      'Tech / AI (Riskli)' → 'tech_ai_riskli'
    """
    # Collapse any run of non-alphanumeric chars into a single '_', then
    # strip leading/trailing underscores to match manually-created folders.
    sector_key = re.sub(r'[^a-z0-9]+', '_', (sector_name or "").lower()).strip('_')
    candidates = [
        os.path.join(BG_MUSIC_DIR, sector_key),
        os.path.join(BG_MUSIC_DIR, "default"),
        BG_MUSIC_DIR,
    ]
    for folder in candidates:
        if not os.path.isdir(folder):
            continue
        tracks = [
            os.path.join(folder, f)
            for f in os.listdir(folder)
            if f.lower().endswith((".mp3", ".wav", ".ogg", ".m4a"))
        ]
        if tracks:
            chosen = random.choice(tracks)
            print(f"[MUSIC] Selected track: {chosen}")
            return chosen
    print(f"[MUSIC] No bg_music tracks found for sector '{sector_name}' — skipping.")
    return None


def mix_audio_with_ducking(
    main_audio,
    tts_audio_clip,
    music_path: Optional[str],
    video_duration: float,
) -> Optional[object]:
    """
    Builds the final mixed audio track:
      - main (original)  : 100%
      - bg music         : 8% globally, ducked to 3% during TTS
      - TTS voiceover    : 100% (volumex already applied upstream)

    Smart ducking strategy:
      We create TWO music clips:
        1. A full-duration clip at 8% volume (global).
        2. A TTS-duration clip at 3% volume starting at t=0 (overlay).
      CompositeAudioClip sums them, so during TTS the effective music
      level becomes 3% + (proportional reduction from the global 8% track
      being replaced). To keep this simple and crash-safe we instead use
      a single music clip that switches volume at tts boundaries using
      `audio_fadein` / `audio_fadeout` tricks — but the simplest reliable
      approach is to composite three clips and let MoviePy sum them:
        - music_global  (full duration, 8%)
        - music_duck_patch (tts duration, -5% = -0.05 additive correction)
      Since MoviePy doesn't support negative volume, we instead:
        build music_base at 3% for the entire duration, then
        add a music_boost clip at 5% for the non-TTS parts.

    Practical implementation (crash-safe):
      Segment the music into three regions:
        [0 .. tts_end]  → 3%  (ducked, during TTS)
        [tts_end .. dur] → 8% (full, after TTS)

    If no TTS, music plays at 8% for the whole duration.
    Returns a CompositeAudioClip padded to video_duration, or None.
    """
    if music_path is None or not os.path.exists(music_path):
        # No music — just mix main + TTS
        if main_audio is None:
            return None
        parts = [main_audio.set_duration(video_duration)]
        if tts_audio_clip is not None:
            parts.append(tts_audio_clip)
        return CompositeAudioClip(parts).set_duration(video_duration)

    try:
        raw_music = AudioFileClip(music_path)
    except Exception as e:
        print(f"[MUSIC] Failed to load '{music_path}': {e}")
        raw_music = None

    if raw_music is None:
        parts = []
        if main_audio is not None:
            parts.append(main_audio.set_duration(video_duration))
        if tts_audio_clip is not None:
            parts.append(tts_audio_clip)
        return CompositeAudioClip(parts).set_duration(video_duration) if parts else None

    # Loop music to cover full video duration
    if raw_music.duration < video_duration:
        loops_needed = int(math.ceil(video_duration / raw_music.duration))
        from moviepy.editor import concatenate_audioclips
        loop_list = [raw_music] * loops_needed
        looped_music = concatenate_audioclips(loop_list).subclip(0, video_duration)
    else:
        looped_music = raw_music.subclip(0, video_duration)

    # Determine TTS duration for ducking window
    tts_dur = 0.0
    if tts_audio_clip is not None:
        try:
            tts_dur = tts_audio_clip.duration
        except Exception:
            tts_dur = 0.0

    # Build ducked / full music segments
    audio_parts = []

    if main_audio is not None:
        audio_parts.append(
            main_audio.set_duration(video_duration)
        )

    if tts_dur > 0:
        # Music ducked region [0 .. tts_dur]
        ducked_seg = looped_music.subclip(0, min(tts_dur, video_duration)).volumex(BG_MUSIC_DUCK_VOL)
        audio_parts.append(ducked_seg)
        # Music at full volume after TTS
        if tts_dur < video_duration:
            full_seg = (
                looped_music
                .subclip(tts_dur, video_duration)
                .volumex(BG_MUSIC_GLOBAL_VOL)
                .set_start(tts_dur)
            )
            audio_parts.append(full_seg)
    else:
        # No TTS — music at full 8% for entire duration
        audio_parts.append(looped_music.volumex(BG_MUSIC_GLOBAL_VOL))

    if tts_audio_clip is not None:
        audio_parts.append(tts_audio_clip)

    return CompositeAudioClip(audio_parts).set_duration(video_duration)

# ============================================================================
# 5. ENCODING — CPU (libx264)
# ============================================================================

def write_clip_cpu(clip, out_name, fps=60):
    """Encode the final clip. Tries NVENC (GPU) first, falls back to libx264 (CPU)."""
    import time as _time
    _t0 = _time.time()
    print(f"[ENCODE] Starting \u2014 duration={clip.duration:.1f}s fps={fps}")

    # ── Attempt 1: NVENC (hardware GPU encoder) ──────────────────────────────
    try:
        print("[ENCODE] Trying h264_nvenc (GPU)...")
        clip.write_videofile(
            out_name,
            fps=fps,
            codec="h264_nvenc",
            audio_codec="aac",
            audio_bitrate="192k",
            preset="p4",           # NVENC quality preset (p1=fastest … p7=best)
            ffmpeg_params=[
                "-rc",        "vbr",
                "-cq",        "23",
                "-b:v",       "8M",
                "-maxrate",   "12M",
                "-bufsize",   "16M",
                "-profile:v", "high",
                "-pix_fmt",   "yuv420p",
                "-tune",      "hq",
                "-movflags",  "+faststart",
            ],
            threads=0,
            logger=None,
        )
        print(f"[ENCODE] \u2713 NVENC encode successful in {_time.time()-_t0:.1f}s: {os.path.basename(out_name)}")
        return
    except Exception as _nvenc_err:
        print(f"[ENCODE] NVENC failed: {_nvenc_err}")
        print("[ENCODE] Falling back to libx264 (CPU)...")

    # ── Attempt 2: libx264 CPU fallback ──────────────────────────────────────
    clip.write_videofile(
        out_name,
        fps=fps,
        codec="libx264",
        audio_codec="aac",
        audio_bitrate="192k",
        preset="ultrafast",
        ffmpeg_params=["-crf", "23", "-threads", "0"],
        logger=None,
    )
    print(f"[ENCODE] \u2713 libx264 (CPU) encode done in {_time.time()-_t0:.1f}s: {os.path.basename(out_name)}")


# ============================================================================
# 5b. PNG FRAME EXPORT HELPER (for FFmpeg overlay use)
# ============================================================================

def _clip_to_png(clip, filepath: str, t: float = 0) -> str:
    """Save a single frame of a MoviePy clip as PNG for FFmpeg overlay use."""
    import numpy as _np
    from PIL import Image as _PilImage
    frame = clip.get_frame(t)
    img = _PilImage.fromarray(frame.astype(_np.uint8))
    img.save(filepath)
    return filepath


# ============================================================================
# 6. ANIMATION HELPER
# ============================================================================

def make_slide_fade(clip, base_y: int, duration: float = SLIDE_FADE_DUR,
                    slide_px: int = SLIDE_OFFSET_PX):
    """
    Returns a MoviePy clip with a Slide-Up + Fade-In entrance animation.
    The clip travels `slide_px` upward over `duration` seconds with an
    ease-out quadratic easing, combined with a crossfade-in.

    Parameters
    ----------
    clip     : MoviePy clip (already set_start / set_duration)
    base_y   : target Y position at rest (int, absolute pixels)
    duration : animation duration seconds
    slide_px : pixels to travel upward
    """
    def _pos_fn(t):
        # Relative time from clip start
        progress = min(t / duration, 1.0) if duration > 0 else 1.0
        eased    = progress * (2 - progress)   # ease-out quad
        cur_y    = int(base_y + slide_px * (1 - eased))
        return ("center", cur_y)

    animated = clip.set_position(_pos_fn).crossfadein(min(duration, clip.duration))
    return animated


# ============================================================================
# 7. SUBSCRIBE BUTTON — 37-40% width, animated slide-up
# ============================================================================

def render_subscribe_button(channel_name: str, theme_color: str,
                            clip_duration: float,
                            video_width: int = OUTPUT_W,
                            video_height: int = OUTPUT_H,
                            start_t: float = 0.0) -> Optional[object]:
    """
    Subscribe button slides in from bottom at start_t (caller passes
    clip_duration*0.70 so it only appears in the final 30% — no clutter
    during the hook/insight/ticker windows).
    """
    if not _HAS_PIL or np is None:
        return None

    btn_w    = int(video_width * SUBSCRIBE_BTN_RATIO)
    btn_h    = max(80, int(btn_w * 0.22))
    bg_color = _hex_to_rgb(theme_color)

    img  = Image.new("RGBA", (btn_w, btn_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    radius = int(btn_h * 0.25)
    draw.rounded_rectangle(
        [(0, 0), (btn_w - 1, btn_h - 1)],
        radius=radius, fill=bg_color + (235,)
    )

    label     = f"▶  Subscribe\n{channel_name[:24]}"
    font_size = max(18, btn_h // 4)
    try:
        font = ImageFont.truetype("impact.ttf", font_size)
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()

    bbox   = draw.multiline_textbbox((0, 0), label, font=font, align="center")
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    draw.multiline_text(
        ((btn_w - text_w) // 2, (btn_h - text_h) // 2),
        label, fill=(255, 255, 255, 255), font=font, align="center"
    )

    arr        = np.array(img)
    btn_x      = (video_width - btn_w) // 2
    base_y     = 1650
    active_dur = max(clip_duration - start_t, 0.5)

    # t is relative to this clip's own start (0 = moment button first appears)
    def _pos_fn(t):
        progress = min(t / SLIDE_FADE_DUR, 1.0)
        eased    = progress * (2 - progress)
        cur_y    = int(base_y + SLIDE_OFFSET_PX * (1 - eased))
        return (btn_x, cur_y)

    btn_final = (
        ImageClip(arr, ismask=False)
        .set_duration(active_dur)
        .set_start(start_t)
        .set_position(_pos_fn)
        .crossfadein(SLIDE_FADE_DUR)
    )

    print(
        f"[BTN] Subscribe button: start={start_t:.1f}s "
        f"({btn_w}x{btn_h}px), slide-up {SLIDE_FADE_DUR}s"
    )
    return btn_final


# ============================================================================
# 7b. DYNAMIC BANNERS — hook_text (0-3s) and technical_insight (mid-video)
# ============================================================================

def render_hook_banner(
    hook_text: str,
    clip_w: int,
    clip_h: int,
    accent_color: str = "#1E90FF",
    accent_color_3: str = "#39FF14",   # neon glow highlight
    font_style: str = "Impact",
    start_t: float = 0.0,
) -> Optional[object]:
    """
    BEAT 1 (t=0-4s): Hook banner only. Bold, centered, glassmorphism.
    - 75% opacity dark glass panel
    - 3px neon left border (accent_color_3)
    - 0.5s slide-in from top, 0.5s fade-out at end
    """
    if not hook_text or not hook_text.strip():
        return None

    banner_h  = 140
    banner_y  = 140
    dur       = 4.0                   # Beat 1: 0-4s
    slide_dur = 0.5
    panel_w   = clip_w - 60
    r3, g3, b3 = _hex_to_rgb(accent_color_3)   # neon glow

    # Ease-in-out slide from top: panel starts 80px above final position
    def _slide_pos(t):
        if t < slide_dur:
            progress = t / slide_dur
            eased    = progress * (2 - progress)   # ease-in-out
            offset   = int((1 - eased) * 80)
        else:
            offset = 0
        return (30, banner_y - offset)

    glass_bg = (
        ColorClip(size=(panel_w, banner_h), color=(12, 12, 18))
        .set_opacity(0.75)
        .set_start(start_t)
        .set_duration(dur)
        .set_position(_slide_pos)
        .crossfadein(0.3)
        .crossfadeout(0.5)
    )

    # 3px neon left border (accent_color_3)
    neon_border = (
        ColorClip(size=(3, banner_h), color=(r3, g3, b3))
        .set_start(start_t)
        .set_duration(dur)
        .set_position(_slide_pos)
        .crossfadein(0.3)
        .crossfadeout(0.5)
    )

    safe_text = hook_text.strip().upper()[:40]
    try:
        txt = (
            _make_text_clip(
                safe_text,
                fontsize=58, font=font_style,
                color="white", stroke_color="black", stroke_width=2,
                method="caption", size=(panel_w - 30, None), align="center",
            )
            .set_start(start_t)
            .set_duration(dur)
            .set_position(("center", banner_y + 22))
            .crossfadein(0.35)
            .crossfadeout(0.5)
        )
        print(f"[HOOK] Beat1 banner: '{safe_text}'  (t={start_t:.1f}-{start_t+dur:.1f}s)")
        return [glass_bg, neon_border, txt]
    except Exception as e:
        print(f"[HOOK] Banner text failed: {e}")
        return [glass_bg, neon_border]


def render_technical_banner(
    technical_insight: str,
    clip_duration: float,
    clip_w: int,
    clip_h: int,
    accent_color: str = "#1E90FF",
    font_style: str = "Impact",
) -> Optional[list]:
    """
    Stage 2 (5-15s): Insight Banner at y=400 (top quarter).
    Glassmorphism: 0.7 opacity dark background + 4px left-border of accent_color.
    Hidden when hook banner is showing (takes over after 5s).
    """
    if not technical_insight or not technical_insight.strip():
        return None

    start_t    = 5.0
    end_t      = min(10.0, clip_duration - 0.2)   # ends at t=10 — tickers start at t=11
    active_dur = end_t - start_t
    if active_dur <= 0:
        return None

    banner_h  = 90
    banner_y  = 1220   # neutral zone — below main video, above bottom zone
    panel_w   = clip_w - 60
    border_w  = 4
    r, g, b   = _hex_to_rgb(accent_color)
    slide_dur = 0.5

    def _slide_up_pos(t):
        if t < slide_dur:
            progress = t / slide_dur
            eased    = progress * (2 - progress)
            y_off    = int((1 - eased) * 60)
        else:
            y_off = 0
        return (30, banner_y + y_off)

    glass_panel = (
        ColorClip(size=(panel_w, banner_h), color=(0, 0, 0))
        .set_opacity(180/255)
        .set_start(start_t)
        .set_duration(active_dur)
        .set_position(_slide_up_pos)
        .crossfadein(0.3)
        .crossfadeout(0.5)
    )

    accent_bar = (
        ColorClip(size=(border_w, banner_h), color=(r, g, b))
        .set_start(start_t)
        .set_duration(active_dur)
        .set_position(_slide_up_pos)
        .crossfadein(0.3)
        .crossfadeout(0.5)
    )

    layers = [glass_panel, accent_bar]

    safe_text = technical_insight.strip()[:140]
    try:
        label_clip = (
            _make_text_clip(
                "INSIGHT",
                fontsize=18, font=font_style,
                color=f"#{r:02X}{g:02X}{b:02X}",
                method="label",
            )
            .set_start(start_t)
            .set_duration(active_dur)
            .set_position(lambda t: (_slide_up_pos(t)[0] + 12, _slide_up_pos(t)[1] + 10))
            .crossfadein(0.3)
            .crossfadeout(0.5)
        )
        layers.append(label_clip)

        txt = (
            _make_text_clip(
                safe_text,
                fontsize=30, font=font_style,
                color="white", stroke_color="black", stroke_width=1,
                method="caption", size=(panel_w - 20, None), align="West",
            )
            .set_start(start_t)
            .set_duration(active_dur)
            .set_position(lambda t: (_slide_up_pos(t)[0] + 12, _slide_up_pos(t)[1] + 34))
            .crossfadein(0.3)
            .crossfadeout(0.5)
        )
        layers.append(txt)
        print(f"[INSIGHT] Banner @ y={banner_y}, t={start_t:.1f}-{end_t:.1f}s: '{safe_text[:50]}...'")
    except Exception as e:
        print(f"[INSIGHT] Banner text failed: {e}")

    return layers


def render_title_hook_card(
    hook_text: str,
    clip_w: int,
    clip_h: int,
    accent_color: str = "#3B82F6",
    font_style: str = "Impact",
    duration: float = 2.0,
) -> list:
    """
    Full-screen 2-second title card at t=0: draws viewer attention before
    the glassmorphism banners appear. Dark overlay + large centered text +
    accent underline bar. Fades in/out cleanly so it doesn't feel jarring.
    """
    if not hook_text or not hook_text.strip():
        return []

    r, g, b   = _hex_to_rgb(accent_color)
    safe_text = hook_text.strip().upper()
    layers    = []

    # Overlay covers raw footage instantly (no fade-in — first frame must be branded)
    overlay = (
        ColorClip(size=(clip_w, clip_h), color=(5, 5, 10))
        .set_opacity(0.88)
        .set_start(0)
        .set_duration(duration)
        .crossfadeout(0.3)
    )
    layers.append(overlay)

    # Thin accent bar under the text
    bar_w = int(clip_w * 0.65)
    bar_y = int(clip_h * 0.57)
    accent_bar = (
        ColorClip(size=(bar_w, 5), color=(r, g, b))
        .set_start(0)
        .set_duration(duration)
        .set_position(((clip_w - bar_w) // 2, bar_y))
        .crossfadeout(0.3)
    )
    layers.append(accent_bar)

    try:
        txt = (
            _make_text_clip(
                safe_text[:60],
                fontsize=92, font="Impact",   # force Impact for max visual punch
                color="white", stroke_color="black", stroke_width=4,
                method="caption", size=(int(clip_w * 0.82), None), align="center",
            )
            .set_start(0)
            .set_duration(duration)
            .set_position(("center", int(clip_h * 0.36)))
            .crossfadeout(0.3)
        )
        layers.append(txt)
        print(f"[TITLE_CARD] Full-screen hook: '{safe_text[:40]}'  ({duration}s)")
    except Exception as e:
        print(f"[TITLE_CARD] Text failed: {e}")

    return layers


def render_branding_overlay(
    channel_name: str,
    clip_duration: float,
    clip_w: int,
    clip_h: int,
) -> Optional[object]:
    """
    Bottom-LEFT branding: "Analysis: {channel_name}"
    Fixed position: x=50, y=1850  (above the 4px progress bar at y=1916).
    Bottom-right area (x > 800) is kept entirely clear for future logos.
    """
    label = f"Analysis: {channel_name or ATTRIBUTION_CHANNEL}"
    try:
        txt = (
            TextClip(
                label,
                fontsize=24, font="Arial",
                color="white", stroke_color="black", stroke_width=1,
                method="label",
            )
            .set_opacity(0.85)
            .set_duration(clip_duration)
            .set_position((50, 1850))
        )
        print(f"[BRAND] Branding overlay (bottom-left): '{label}'")
        return txt
    except Exception as e:
        print(f"[BRAND] Branding render failed: {e}")
        return None


def render_attribution_overlay(
    original_handle: str,
    clip_duration: float,
    clip_w: int,
    clip_h: int,
) -> Optional[object]:
    """
    TOP-RIGHT attribution: "Original Footage: @{handle}"
    Small, low-opacity, placed at top-right so it stays clear of the
    bottom-left branding and leaves bottom-right empty for channel logos.
    """
    handle = (original_handle or "").lstrip("@").strip()
    if not handle:
        return None
    label = f"Original Footage: @{handle}"
    try:
        txt = (
            TextClip(
                label,
                fontsize=20, font="Arial",
                color="white", stroke_color="black", stroke_width=1,
                method="label",
            )
            .set_opacity(0.65)
            .set_duration(clip_duration)
            .set_position(("right", 30))   # top-right, 30px from top
        )
        print(f"[ATTR] Attribution overlay (top-right): '{label}'")
        return txt
    except Exception as e:
        print(f"[ATTR] Attribution render failed: {e}")
        return None


def render_progress_bar(
    clip_duration: float,
    clip_w: int,
    clip_h: int,
    accent_color: str = "#39FF14",
    bar_height: int = 3,
) -> Optional[object]:
    """
    A 4-px progress bar that fills left-to-right in real time.
    Fixed at y=1916 (absolute bottom of 1920px frame).
    Uses a make_frame lambda for per-frame width computation.
    """
    if np is None:
        return None

    r, g, b = _hex_to_rgb(accent_color)
    bar_y   = 0   # top edge of screen (3-zone layout: visible above all content)

    def _make_frame(t):
        fraction = min(t / max(clip_duration, 0.001), 1.0)
        filled_w = max(int(fraction * clip_w), 1)
        frame    = np.zeros((bar_height, clip_w, 3), dtype=np.uint8)
        frame[:, :filled_w] = [r, g, b]
        return frame

    from moviepy.video.VideoClip import VideoClip
    bar_clip = (
        VideoClip(_make_frame, duration=clip_duration)
        .set_position((0, bar_y))
    )
    print(f"[PROGRESS] Progress bar: {clip_w}x{bar_height}px @ y={bar_y}  color={accent_color}")
    return bar_clip


# ============================================================================
# 8. INFOBOX OVERLAY — Zone 1  (top 15%, hard time windows, never overlaps)
# ============================================================================

def _smart_truncate(text: str, max_chars: int) -> str:
    """Truncate at last complete word boundary, append ellipsis."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    boundary = cut.rfind(' ')
    return (cut[:boundary] if boundary > max_chars // 2 else cut) + "…"


def render_infobox_overlay(
    clip_duration: float, clip_w: int, clip_h: int,
    infobox_1: str, infobox_2: str,
    theme_color: str = "#FF0000",
    accent_color_1: str = "#1E90FF",
    accent_color_2: str = "#FF6D00",
    accent_color_3: str = "#39FF14",
) -> list:
    """
    Alternating single-ticker overlays in top zone (y=ZONE1_Y_ANCHOR).
    Hard time windows — ticker_1 t=11-18, ticker_2 t=19-25.
    Never active simultaneously. Text is word-boundary truncated.
    """
    layers = []
    if not infobox_1 and not infobox_2:
        return layers

    box_y  = ZONE1_Y_ANCHOR
    box_h  = 90
    # Hard windows (user spec): insight ends t=10, tickers start t=11
    t1_start = min(11.0, clip_duration * 0.41)
    t1_end   = min(18.0, max(t1_start + 2.0, clip_duration * 0.67))
    t2_start = min(19.0, clip_duration * 0.70)
    t2_end   = min(25.0, max(t2_start + 2.0, clip_duration * 0.93))
    slide_dur = 0.5
    r3, g3, b3 = _hex_to_rgb(accent_color_3)

    def _beat2_ticker(text: str, color_hex: str, t_start: float, t_end: float, icon: str):
        if not text:
            return
        dur = max(min(t_end, clip_duration - 0.25) - t_start, 1.0)
        if dur <= 0:
            return

        r, g, b = _hex_to_rgb(color_hex)
        panel_w = clip_w - 60

        # Ease-in-out slide from left
        def _pos(t):
            if t < slide_dur:
                progress = t / slide_dur
                eased    = progress * (2 - progress)
                x_off    = int((1 - eased) * (panel_w + 40))
            else:
                x_off = 0
            return (30 - x_off, box_y)

        bg = (
            ColorClip(size=(panel_w, box_h), color=(12, 12, 18))
            .set_opacity(0.75)
            .set_start(t_start)
            .set_duration(dur)
            .set_position(_pos)
            .crossfadein(0.3)
            .crossfadeout(0.5)
        )
        layers.append(bg)

        # 3px neon bottom border
        neon_bar = (
            ColorClip(size=(panel_w, 3), color=(r3, g3, b3))
            .set_start(t_start)
            .set_duration(dur)
            .set_position(lambda t: (_pos(t)[0], box_y + box_h - 3))
            .crossfadein(0.3)
            .crossfadeout(0.5)
        )
        layers.append(neon_bar)

        # Accent left stripe (channel color for identity)
        accent_stripe = (
            ColorClip(size=(4, box_h), color=(r, g, b))
            .set_start(t_start)
            .set_duration(dur)
            .set_position(_pos)
        )
        layers.append(accent_stripe)

        safe = f"{icon} {text[:68]}"
        try:
            txt = (
                _make_text_clip(
                    safe, fontsize=36, font="Impact",
                    color="white",
                    stroke_color="black", stroke_width=1,
                    method="caption", size=(panel_w - 20, None), align="West",
                )
                .set_start(t_start)
                .set_duration(dur)
                .set_position(lambda t: (_pos(t)[0] + 12, box_y + 20))
                .crossfadein(0.3)
                .crossfadeout(0.5)
            )
            layers.append(txt)
        except Exception as e:
            print(f"[TICKER] Text render failed: {e}")

        print(f"[TICKER] '{icon}' t={t_start:.0f}-{t_start+dur:.0f}s: '{text[:40]}'")

    # Hard time windows — never overlap simultaneously
    if infobox_1:
        _beat2_ticker(_smart_truncate(infobox_1, 60), accent_color_1, t_start=t1_start, t_end=t1_end, icon="◉")
    if infobox_2:
        _beat2_ticker(_smart_truncate(infobox_2, 60), accent_color_2, t_start=t2_start, t_end=t2_end, icon="⚡")

    return layers


# ============================================================================
# 9. KARAOKE CAPTIONS — Zone 2  (bottom 30%, phrase groups + pill backgrounds)
# ============================================================================

def _group_words_into_phrases(word_segments: List[dict],
                               words_per_phrase: int = 4) -> List[dict]:
    """Groups Whisper word-level segments into short display phrases."""
    groups = []
    for i in range(0, len(word_segments), words_per_phrase):
        chunk = word_segments[i:i + words_per_phrase]
        text  = " ".join(
            (w.get("word") or w.get("text") or "").strip() for w in chunk
        ).strip()
        if not text:
            continue
        start = float(chunk[0].get("start", 0))
        end   = float(chunk[-1].get("end", start + 0.5))
        if end <= start:
            end = start + 0.3 * len(chunk)
        groups.append({"text": text, "start": start, "end": end})
    return groups


def _make_phrase_pill_clip(text: str, duration: float,
                            font_size: int = 58,
                            max_width: int = 960) -> Optional[object]:
    """PIL-rendered phrase with semi-transparent rounded-rectangle pill background."""
    if not _HAS_PIL or np is None:
        return None
    text = text.strip()
    if not text:
        return None

    try:
        font = ImageFont.truetype("impact.ttf", font_size)
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()

    dummy_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox   = dummy_draw.textbbox((0, 0), text, font=font)
    text_w = min(bbox[2] - bbox[0], max_width - 64)
    text_h = bbox[3] - bbox[1]

    pad_x, pad_y = 32, 16
    pill_w = text_w + pad_x * 2
    pill_h = text_h + pad_y * 2

    img  = Image.new("RGBA", (pill_w, pill_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([(0, 0), (pill_w - 1, pill_h - 1)],
                            radius=pill_h // 2, fill=(0, 0, 0, 204))
    draw.text((pad_x, pad_y), text, fill=(255, 255, 255, 255), font=font)

    return ImageClip(np.array(img), ismask=False).set_duration(duration)


def create_karaoke_subtitles_clip(whisper_segments: List[dict],
                                   clip_w: int, clip_h: int,
                                   clip_duration: float,
                                   theme_color: str = "#39FF14") -> list:
    """
    Zone 2 caption overlay. Returns a FLAT LIST of pill ImageClips with
    .start and .duration already set. Returns [] if no subtitles.
    Caller is responsible for adding these to the master all_layers list.
    """
    if not whisper_segments:
        return []

    effective_duration = max(clip_duration - 0.2, 0.1)
    W        = clip_w
    y_anchor = ZONE2_Y_ANCHOR

    groups      = _group_words_into_phrases(whisper_segments, words_per_phrase=4)
    subs_layers = []

    for g in groups:
        g_start = min(float(g["start"]), effective_duration - 0.05)
        g_end   = min(float(g["end"]),   effective_duration)
        dur     = max(g_end - g_start, 0.1)
        if g_start >= effective_duration:
            continue

        pill = _make_phrase_pill_clip(g["text"], duration=dur)
        if pill is not None:
            try:
                pill_x = (W - pill.w) // 2
                fade   = min(0.12, dur * 0.25)
                subs_layers.append(
                    pill
                    .set_start(g_start)
                    .set_position((pill_x, y_anchor))
                    .crossfadein(fade)
                    .crossfadeout(fade)
                )
            except Exception as e:
                print(f"[KARAOKE] Pill render failed for '{g['text'][:20]}': {e}")
        else:
            # Fallback to plain TextClip when PIL unavailable
            try:
                txt = (
                    TextClip(
                        g["text"], fontsize=58, font="Impact",
                        color="white", stroke_color="black", stroke_width=4,
                        method="caption", size=(W - 80, None), align="center",
                    )
                    .set_start(g_start)
                    .set_duration(dur)
                    .set_position(("center", y_anchor))
                    .crossfadein(0.08)
                )
                subs_layers.append(txt)
            except Exception as e:
                print(f"[KARAOKE] Fallback text failed: {e}")

    if subs_layers:
        print(f"[KARAOKE] ✓ {len(groups)} phrase groups ({len(whisper_segments)} words) → {len(subs_layers)} pills.")
    return subs_layers


# ============================================================================
# 9b. ANNOTATION PILLS — branded pill in BOTTOM ZONE (never inside clip bounds)
# ============================================================================

def render_annotation_pill(
    label: str,
    start_t: float,
    end_t: float,
    clip_w: int,
    clip_h: int,
    accent_color_1: str = "#1E90FF",
    font_size: int = 42,
) -> Optional[object]:
    """
    Renders a behaviour annotation (e.g. 'LOOKS DOWN', 'LOOKS AWAY', 'SMILES')
    as a branded pill in the BOTTOM ZONE — below the main video, above captions.

    Design contract:
      - Shape  : rounded-rectangle pill (same radius formula as subtitle pills)
      - Color  : accent_color_1 (brand color, 78 % opacity) — NOT red
      - Zone   : y = ZONE2_Y_ANCHOR - pill_h - 12  (always below main video)
      - Icon   : emoji/arrow prefix derived from label semantics
      - Font   : ~42 px — slightly smaller than karaoke subtitle pills
    """
    if not _HAS_PIL or np is None:
        return None
    label = (label or "").strip()
    if not label:
        return None

    dur = max(end_t - start_t, 0.5)

    # ── Icon assignment ───────────────────────────────────────────────────────
    lc = label.lower()
    if "down" in lc:
        icon = "↓"
    elif "away" in lc or "side" in lc:
        icon = "↗"
    elif "smile" in lc or "laugh" in lc or "grin" in lc:
        icon = "☺"
    elif "look" in lc or "eye" in lc or "gaze" in lc:
        icon = "👁"
    else:
        icon = "•"

    display_text = f"{icon}  {label.upper()}"

    try:
        try:
            font = ImageFont.truetype("arial.ttf", font_size)
        except Exception:
            try:
                font = ImageFont.truetype("impact.ttf", font_size)
            except Exception:
                font = ImageFont.load_default()

        dummy  = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        bbox   = dummy.textbbox((0, 0), display_text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        pad_x, pad_y = 28, 12
        pill_w = min(text_w + pad_x * 2, clip_w - 60)
        pill_h = text_h + pad_y * 2

        r, g, b = _hex_to_rgb(accent_color_1)
        img  = Image.new("RGBA", (pill_w, pill_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # Full-radius pill — identical to subtitle pill style
        draw.rounded_rectangle(
            [(0, 0), (pill_w - 1, pill_h - 1)],
            radius=pill_h // 2,
            fill=(r, g, b, 200),
        )
        draw.text((pad_x, pad_y), display_text, fill=(255, 255, 255, 255), font=font)

        arr    = np.array(img)
        pill_x = (clip_w - pill_w) // 2
        # Bottom zone: just above the karaoke subtitle line
        pill_y = ZONE2_Y_ANCHOR - pill_h - 12

        print(f"[ANNOT] '{display_text}' @ y={pill_y}  t={start_t:.1f}-{end_t:.1f}s")
        return (
            ImageClip(arr, ismask=False)
            .set_duration(dur)
            .set_start(start_t)
            .set_position((pill_x, pill_y))
            .crossfadein(0.12)
            .crossfadeout(0.12)
        )
    except Exception as e:
        print(f"[ANNOT] Pill render failed for '{label}': {e}")
        return None


def generate_outro_segment(
    cta_question: str,
    last_frame: "np.ndarray",
    clip_w: int,
    clip_h: int,
    voice_id: str,
    tts_dir: str,
    theme_color: str = "#FF0000",
    preset_audio_path: Optional[str] = None,
    judgment: Optional[str] = None,
    font_style: str = "Impact",
    accent_color_1: str = "#1E90FF",
    accent_color_3: str = "#39FF14",
    outro_bg_clip=None,    # moving video background (last few seconds of original)
) -> Optional[object]:
    """
    Modern 900px Glassmorphism Outro Card.
    - Centered 900px card (not full-width)
    - "ANALYSIS" mini-tag at top with neon accent_color_3
    - Judgment text (top half) + CTA question (bottom half)
    - Card fades out after 4s, pulsing subscribe CTA takes over
    - Dark overlay + double-blur on last frame background
    YPP compliance: never skipped, falls back to silent text-only outro.
    """
    if not cta_question or not cta_question.strip():
        return None

    # ── 1. Resolve TTS audio (pre-generated takes priority over internal Kokoro) ─
    outro_audio = None
    outro_dur   = 5.0   # fallback duration

    if preset_audio_path and os.path.exists(preset_audio_path):
        try:
            outro_audio = AudioFileClip(preset_audio_path)
            outro_dur   = outro_audio.duration
            print(f"[OUTRO] ✓ Pre-generated TTS loaded: {outro_dur:.2f}s  file={preset_audio_path}")
        except Exception as load_err:
            print(f"[OUTRO] Pre-gen audio load failed: {load_err} — falling back to Kokoro.")
            outro_audio = None

    if outro_audio is None:
        tts_out = os.path.join(tts_dir, f"outro_{uuid.uuid4().hex[:8]}.wav")
        # Chinese character guard — Kokoro cannot synthesise CJK text
        def _has_chinese_local(text):
            return any('\u4e00' <= c <= '\u9fff' for c in (text or ''))
        if _has_chinese_local(cta_question):
            print(f"[TTS] Chinese detected in cta_question, using fallback text")
            cta_question = "What do you think? Subscribe for more insights!"
        tts_ok  = generate_kokoro_tts(cta_question, voice_id, tts_out)
        if tts_ok and os.path.exists(tts_out):
            try:
                outro_audio = AudioFileClip(tts_out)
                outro_dur   = outro_audio.duration
                print(f"[OUTRO] ✓ Kokoro TTS: {outro_dur:.2f}s  voice={voice_id}")
            except Exception as load_err:
                print(f"[OUTRO] Kokoro audio load failed: {load_err} — silent outro.")
                outro_audio = None
                outro_dur   = 5.0
        else:
            print("[OUTRO] Kokoro TTS failed — silent text-only outro (5 s).")
            outro_audio = None
            outro_dur   = 5.0

    # ── 2. Background — moving clip preferred (keeps outro alive), static fallback ─
    bg_composite = None

    if outro_bg_clip is not None:
        try:
            # Loop the clip if it's shorter than outro_dur
            if outro_bg_clip.duration < outro_dur:
                from moviepy.editor import concatenate_videoclips as _catv
                loops = int(math.ceil(outro_dur / outro_bg_clip.duration)) + 1
                bg_anim = _catv([outro_bg_clip] * loops).subclip(0, outro_dur)
            else:
                bg_anim = outro_bg_clip.subclip(0, outro_dur)
            bg_anim = bg_anim.resize((clip_w, clip_h))
            dark_ov = (
                ColorClip(size=(clip_w, clip_h), color=(0, 0, 0))
                .set_opacity(0.60)
                .set_duration(outro_dur)
            )
            bg_composite = CompositeVideoClip([bg_anim, dark_ov], size=(clip_w, clip_h))
            print("[OUTRO] Moving background: last-clip replay loaded.")
        except Exception as e:
            print(f"[OUTRO] Moving bg failed ({e}) — using static fallback.")
            bg_composite = None

    if bg_composite is None:
        if last_frame is not None and cv2 is not None:
            blurred = cv2.GaussianBlur(last_frame, (201, 201), 0)
        else:
            blurred = last_frame
        if blurred is not None:
            bg_clip = ImageClip(blurred).set_duration(outro_dur).resize((clip_w, clip_h))
            dark_ov = ColorClip(size=(clip_w, clip_h), color=(0,0,0)).set_opacity(0.65).set_duration(outro_dur)
            bg_composite = CompositeVideoClip([bg_clip, dark_ov], size=(clip_w, clip_h))
        else:
            bg_composite = ColorClip(size=(clip_w, clip_h), color=(10, 10, 10)).set_duration(outro_dur)

    # ── 3. Modern 900px Glassmorphism Card (centered) ─────────────────────────────
    card_w        = min(900, clip_w - 80)       # 900px centered card
    card_h        = 340
    card_x        = (clip_w - card_w) // 2
    card_y        = 790
    fade_in_dur   = 0.5
    # FIX 1: card persists until 0.2 s before outro ends — never disappears while TTS plays
    card_dur = max(outro_dur - 0.2, 4.5)
    r1, g1, b1    = _hex_to_rgb(accent_color_1)
    r3, g3, b3    = _hex_to_rgb(accent_color_3)

    layers = [bg_composite]

    try:
        glass_bg = (
            ColorClip(size=(card_w, card_h), color=(12, 12, 18))
            .set_opacity(0.75)
            .set_duration(card_dur)
            .set_position((card_x, card_y))
            .crossfadein(fade_in_dur)
            .crossfadeout(0.5)
        )
        layers.append(glass_bg)

        # 3px neon BOTTOM border (accent_color_3)
        neon_bottom = (
            ColorClip(size=(card_w, 3), color=(r3, g3, b3))
            .set_duration(card_dur)
            .set_position((card_x, card_y + card_h - 3))
            .crossfadein(fade_in_dur)
            .crossfadeout(0.5)
        )
        layers.append(neon_bottom)

        # 3px neon left border (accent_color_1)
        left_border = (
            ColorClip(size=(3, card_h), color=(r1, g1, b1))
            .set_duration(card_dur)
            .set_position((card_x, card_y))
            .crossfadein(fade_in_dur)
            .crossfadeout(0.5)
        )
        layers.append(left_border)
    except Exception as e:
        print(f"[OUTRO] Glass panel failed: {e}")

    # ── 4. "ANALYSIS" mini-tag header ───────────────────────────────────────────
    try:
        tag_clip = (
            _make_text_clip(
                "■ ANALYSIS",
                fontsize=18, font=font_style,
                color=f"#{r3:02X}{g3:02X}{b3:02X}",
                method="label",
            )
            .set_duration(card_dur)
            .set_position((card_x + 16, card_y + 10))
            .crossfadein(fade_in_dur)
            .crossfadeout(0.5)
        )
        layers.append(tag_clip)
    except Exception as e:
        print(f"[OUTRO] Analysis tag failed: {e}")

    # ── 4. Judgment text — fades out with the card ───────────────────────────
    safe_judgment = (judgment or "").strip()[:200]
    if safe_judgment:
        try:
            judgment_clip = (
                _make_text_clip(
                    safe_judgment,
                    fontsize=44, font=font_style,
                    color="white", stroke_color="black", stroke_width=2,
                    method="caption", size=(card_w - 40, None), align="center",
                )
                .set_duration(card_dur)
                .set_position((card_x + 20, card_y + 24))
                .crossfadein(fade_in_dur)
                .crossfadeout(0.5)
            )
            layers.append(judgment_clip)
            print(f"[OUTRO] Judgment rendered ({card_dur:.1f}s): '{safe_judgment[:50]}...'")
        except Exception as e:
            print(f"[OUTRO] Judgment text failed: {e}")

    # ── 5. CTA question — fades out with the card ────────────────────────────
    safe_question = cta_question.strip()
    try:
        question_clip = (
            _make_text_clip(
                safe_question,
                fontsize=34, font=font_style,
                color="white", stroke_color="black", stroke_width=2,
                method="caption", size=(card_w - 40, None), align="center",
            )
            .set_duration(card_dur)
            .set_position((card_x + 20, card_y + card_h - 130))
            .crossfadein(fade_in_dur)
            .crossfadeout(0.5)
        )
        layers.append(question_clip)
    except Exception as e:
        print(f"[OUTRO] Question text failed: {e}")

    # ── 7. Assemble outro ─────────────────────────────────────────────────────
    outro_video = CompositeVideoClip(layers, size=(clip_w, clip_h))
    outro_video = outro_video.set_duration(outro_dur)

    if outro_audio is not None:
        outro_audio_safe = CompositeAudioClip(
            [outro_audio.set_start(0)]
        ).set_duration(outro_dur)
        outro_video = outro_video.set_audio(outro_audio_safe)

    print(
        f"[OUTRO] card={card_dur:.1f}s  "
        f"judgment={bool(safe_judgment)}  total={outro_dur:.2f}s"
    )
    return outro_video


# ============================================================================
# 11. MAIN RENDERER — generate_vertical_short()
# ============================================================================

def generate_vertical_short(
    video_path: str,
    spec: dict,
    index: int,
    # Only commonly-used params retained
    channel_name:      str           = "My Channel",
    sector_name:       str           = "default",
    original_handle:   str           = None,
    accent_color_1:    str           = "#1E90FF",
    **kwargs,          # Accept but ignore YouTube-specific params
) -> str:
    """
    Simplified entry point for IG Reels rendering.
    Delegates to render_raw_ffmpeg directly.
    YouTube-specific params (infobox, hook_text, cta, etc.) are accepted but ignored.
    """
    from ffmpeg_renderer import render_raw_ffmpeg
    raw_path, ass_path, clip_dur, elapsed = render_raw_ffmpeg(
        video_path=video_path,
        spec=spec,
        index=index,
        sector_name=sector_name,
        original_handle=original_handle,
        _transcribe_fn=transcribe_subclip_for_karaoke,
        _pick_bgm_fn=pick_bg_music,
    )
    return raw_path, elapsed

def main():
    total_start = time.time()

    ap = argparse.ArgumentParser(description="UTube AI Hybrid Video Processor")
    ap.add_argument("--video",  default="", help="Path to local video file")
    ap.add_argument("--yt-url", default="", help="YouTube URL to download and process")
    ap.add_argument("--theme",  default="#39FF14", help="Brand color hex (default: green)")
    ap.add_argument("--voice",  default="af_sky",  help="Kokoro voice ID for outro TTS")
    ap.add_argument("--channel",default="My Channel", help="Channel name for subscribe button")
    args = ap.parse_args()

    ensure_dirs()

    video_path = args.video
    if args.yt_url:
        print(f"[INFO] Downloading: {args.yt_url}")
        try:
            video_path = download_youtube_video(args.yt_url)
        except Exception as e:
            print(f"[ERROR] Download failed: {e}")
            return

    if not video_path or not os.path.exists(video_path):
        print("[ERROR] No video file found. Use --video or --yt-url.")
        return

    segments = split_video_into_segments(video_path)
    if not segments:
        print("[ERROR] No segments produced.")
        return

    system_content = """\
You are a viral clip extractor. Scan the transcript and output ONLY clip timestamps.
No analysis. No explanation. No prose.

SCORING (0-9):
0-6 = boring/skip. NEVER include these.
7   = mildly interesting.
8   = high-energy, surprising, or funny.
9   = TOP 1%: shock, scream, massive fail, hysteria.

OUTPUT FORMAT — one line per clip, nothing else:
INDEX/SCORE/START - END
Example: 1/8/00:15 - 00:37

Rules: duration 20-25 s. Top 3 clips. Zero extra text."""

    short_global_idx = 0

    for i, segment_path in enumerate(segments, 1):
        cleanup_gpu()
        print(f"\n{'='*60}")
        print(f"  SEGMENT {i}/{len(segments)}: {os.path.basename(segment_path)}")
        print(f"{'='*60}")

        try:
            transcript = generate_segment_transcript(segment_path)
            if not transcript:
                print("[SKIP] Empty transcript.")
                continue

            safe_transcript = _truncate_transcript(transcript)
            user_content = (
                f"Transcript:\n{safe_transcript}\n\n"
                f"Find the top 3 viral clips (20-25 s each)."
            )
            messages = [
                {"role": "system", "content": system_content},
                {"role": "user",   "content": user_content},
            ]

            found_intervals = []
            max_retries     = 3

            for attempt in range(max_retries):
                if len(found_intervals) >= 3:
                    break

                print(f"[QWEN] Attempt {attempt+1}/{max_retries}")
                reply = send_prompt_to_qwen(messages, max_tokens=1500)
                print(f"[QWEN REPLY]\n{reply}\n")

                for line in reply.splitlines():
                    if len(found_intervals) >= 3:
                        break

                    match = re.search(
                        r"(\d+)/(\d+)/(\d+[:.]\d+)\s*-\s*(\d+[:.]\d+)", line
                    )
                    if not match:
                        continue

                    score = int(match.group(2))
                    start = parse_timecode(match.group(3))
                    end   = parse_timecode(match.group(4))
                    dur   = end - start

                    if not (20 <= dur <= 25):
                        continue
                    if is_overlapping(start, found_intervals):
                        continue

                    # Metadata analysis
                    ai_meta = analyze_clip_with_qwen(transcript, "general")

                    spec = {"start": start, "end": end, "score": score}
                    try:
                        out = generate_vertical_short(
                            segment_path, spec, short_global_idx,
                            infobox_1=ai_meta.get("infobox_1"),
                            infobox_2=ai_meta.get("infobox_2"),
                            channel_name=args.channel,
                            theme_color=args.theme,
                            end_hook=ai_meta.get("end_hook"),
                            cta_question=ai_meta.get("outro_question"),
                            voice_id=args.voice,
                        )
                        found_intervals.append(start)
                        short_global_idx += 1
                        print(f"[✓] Short saved: {out}")
                    except Exception as e:
                        print(f"[RENDER ERROR] {e}")
                        traceback.print_exc()

        except Exception as e:
            print(f"[ERROR] Segment {i} critical: {e}")
            traceback.print_exc()

        cleanup_gpu()

    elapsed = time.time() - total_start
    h, rem  = divmod(int(elapsed), 3600)
    m, s    = divmod(rem, 60)
    print(f"\n{'='*60}")
    print(f"[INFO] All done.  Elapsed: {h}h {m}m {s}s")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()