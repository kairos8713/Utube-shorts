"""
production.py — batch clip production.

Strict batch order (one model in VRAM at a time):
    1. all downloads            (no GPU; polite random delays; stop on bot-block)
    2. all Whisper              (one WhisperSession, GPU lock held for the stage)
    3. all Qwen                 (scoring + captions, GPU lock for the stage, then unload)
    4. render + RIFE per clip   (GPU lock per clip so other jobs can interleave)
    5. QC → cover/phash hook → on_clip_ready() (persistence lives in the caller)

Everything external is injectable, so the pipeline is tested with fakes and the
end-to-end DRY_RUN can swap in synthetic footage / the mock LLM.
"""
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import downloader
import ffmpeg_renderer
import qwen_specialized
import render_qc
import rife_interpolator
from clip_bounds import align_clip
from config import settings
from gpu_lock import gpu_job
from llm_client import get_llm
from log_setup import get_logger
from media_utils import cleanup_gpu, duration_of, pick_bg_music, seconds_to_timecode, split_video_into_segments

log = get_logger("production")

TARGET_LANGUAGE = "en"


@dataclass
class SourceVideo:
    url: str
    niche: str
    source_id: Optional[int] = None
    source_handle: str = ""
    source_name: str = ""
    music_slug: Optional[str] = None
    max_clips: int = 3
    priority: int = 0
    local_path: Optional[str] = None       # pre-downloaded / synthetic file
    # filled by the pipeline
    path: Optional[str] = None
    segments: list = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class ClipCandidate:
    source: SourceVideo
    segment: dict
    start: float                 # seconds within the segment
    end: float
    score: int
    topic: str = ""
    reason: str = ""
    time_sensitive: bool = False
    words: list = field(default_factory=list)       # relative to clip start
    transcript: str = ""
    caption: Optional[dict] = None
    out_path: Optional[str] = None
    render_info: dict = field(default_factory=dict)
    qc: Optional[render_qc.QCResult] = None
    extras: dict = field(default_factory=dict)      # cover / phash from post_render hook

    @property
    def abs_start(self) -> float:
        return self.segment["offset"] + self.start

    @property
    def abs_end(self) -> float:
        return self.segment["offset"] + self.end


@dataclass
class BatchResult:
    produced: List[ClipCandidate] = field(default_factory=list)
    failed: List[ClipCandidate] = field(default_factory=list)
    source_errors: List[SourceVideo] = field(default_factory=list)
    blocked: Optional[str] = None
    stage_seconds: dict = field(default_factory=dict)


def segment_transcript_text(whisper_segments: list) -> str:
    return "\n".join(f"[{seconds_to_timecode(s['start'])} - {seconds_to_timecode(s['end'])}] "
                     f"{(s.get('text') or '').strip()}" for s in whisper_segments)


def words_from_result(res: dict) -> list:
    out = []
    for seg in res.get("segments", []):
        for w in seg.get("words", []) or []:
            out.append({"start": float(w.get("start", 0)), "end": float(w.get("end", 0)),
                        "text": (w.get("word") or "").strip()})
    return out


def clip_words(words: list, start: float, end: float) -> list:
    return [{"start": round(w["start"] - start, 3), "end": round(min(w["end"], end) - start, 3),
             "text": w["text"]}
            for w in words if w["start"] >= start - 0.05 and w["end"] <= end + 0.3 and w["text"]]


class Pipeline:
    def __init__(self, *, llm=None, whisper_factory=None, download_fn=None, split_fn=None,
                 render_fn=None, interpolate_fn=None, qc_fn=None,
                 is_duplicate: Callable = None, previous_captions: Callable = None,
                 performance_summary: Callable = None, post_render: Callable = None,
                 on_clip_ready: Callable = None, on_clip_failed: Callable = None,
                 delay_fn: Callable = None, out_dir: str = None):
        from transcriber import WhisperSession
        self.llm = llm or get_llm()
        self.whisper_factory = whisper_factory or WhisperSession
        self.download_fn = download_fn or downloader.download_youtube_video
        self.split_fn = split_fn or split_video_into_segments
        self.render_fn = render_fn or ffmpeg_renderer.render_reel
        self.interpolate_fn = interpolate_fn if interpolate_fn is not None else rife_interpolator.apply_rife_interpolation
        self.qc_fn = qc_fn or render_qc.check_video
        self.is_duplicate = is_duplicate or (lambda url, s, e: False)
        self.previous_captions = previous_captions or (lambda niche: [])
        self.performance_summary = performance_summary or (lambda source: "")
        self.post_render = post_render or (lambda cand: {})
        self.on_clip_ready = on_clip_ready or (lambda cand: None)
        self.on_clip_failed = on_clip_failed or (lambda cand, reason: None)
        self.delay_fn = delay_fn if delay_fn is not None else downloader.polite_delay
        self.out_dir = out_dir or settings.REELS_DIR

    # ── 1. downloads ─────────────────────────────────────────────────────────
    def stage_download(self, sources: List[SourceVideo], result: BatchResult) -> List[SourceVideo]:
        ready = []
        for i, src in enumerate(sources):
            if src.local_path:
                src.path = src.local_path
            else:
                if i > 0 and self.delay_fn:
                    self.delay_fn()
                try:
                    src.path = self.download_fn(src.url)
                except downloader.YouTubeBlocked as e:
                    result.blocked = e.kind
                    src.error = str(e)
                    result.source_errors.append(src)
                    log.error("YouTube blocked downloads (%s) — stopping download stage", e.kind)
                    break
                except downloader.DownloadError as e:
                    src.error = str(e)
                    result.source_errors.append(src)
                    continue
            try:
                segs = self.split_fn(src.path)
            except Exception as e:
                src.error = f"split failed: {e}"
                result.source_errors.append(src)
                continue
            for s in segs:
                if "duration" not in s:
                    s["duration"] = duration_of(s["path"])
            src.segments = segs
            ready.append(src)
        return ready

    # ── 2. whisper ───────────────────────────────────────────────────────────
    def stage_whisper(self, sources: List[SourceVideo], result: BatchResult) -> List[SourceVideo]:
        kept = []
        with gpu_job("whisper"):
            with self.whisper_factory() as ws:
                for src in sources:
                    ok = True
                    for seg in src.segments:
                        res = ws.transcribe(seg["path"], words=True)
                        lang = res.get("language") or TARGET_LANGUAGE
                        if lang != TARGET_LANGUAGE:
                            src.error = f"language {lang} != {TARGET_LANGUAGE}"
                            ok = False
                            break
                        seg["whisper_segments"] = res.get("segments", [])
                        seg["text"] = segment_transcript_text(seg["whisper_segments"])
                        seg["words"] = words_from_result(res)
                    if ok:
                        kept.append(src)
                    else:
                        result.source_errors.append(src)
        cleanup_gpu()
        return kept

    # ── 3. qwen ──────────────────────────────────────────────────────────────
    def stage_qwen(self, sources: List[SourceVideo]) -> List[ClipCandidate]:
        chosen = []
        with gpu_job("qwen"):
            try:
                for src in sources:
                    perf = self.performance_summary(src)
                    cands = []
                    for seg in src.segments:
                        if not seg.get("text"):
                            continue
                        for c in qwen_specialized.score_clips(seg["text"], src.niche, seg["duration"],
                                                              perf, llm=self.llm):
                            bounds, why = align_clip(seg.get("words") or [], c["start"], c["end"], seg["duration"])
                            if bounds is None:
                                log.info("clip rejected %s @%.0f-%.0f: %s", src.url, seg["offset"] + c["start"],
                                         seg["offset"] + c["end"], why)
                                continue
                            cand = ClipCandidate(src, seg, bounds[0], bounds[1], c["score"], c["topic"],
                                                 c["reason"], c["time_sensitive"])
                            if self.is_duplicate(src.url, cand.abs_start, cand.abs_end):
                                log.info("skip duplicate segment %s %.0f-%.0f", src.url, cand.abs_start, cand.abs_end)
                                continue
                            cands.append(cand)
                    cands.sort(key=lambda c: -c.score)
                    picked = []
                    for cand in cands:                 # best first; a rejected one makes room for the next
                        if len(picked) >= src.max_clips:
                            break
                        if any(o.segment is cand.segment and cand.start < o.end and o.start < cand.end
                               for o in picked):
                            log.info("clip rejected %s @%.0f: overlaps a better clip after alignment",
                                     src.url, cand.abs_start)
                            continue
                        cand.words = clip_words(seg_words(cand), cand.start, cand.end)
                        cand.transcript = " ".join(w["text"] for w in cand.words)
                        cand.caption = qwen_specialized.write_caption(
                            cand.transcript or cand.segment.get("text", ""), src.niche, src.source_handle,
                            self.previous_captions(src.niche), source_name=src.source_name, llm=self.llm)
                        if cand.caption:
                            picked.append(cand)
                        else:
                            log.warning("no acceptable caption for %s @%.0f", src.url, cand.abs_start)
                    chosen += picked
            finally:
                self.llm.unload()
        return chosen

    # ── 4/5. render + QC ─────────────────────────────────────────────────────
    def stage_render(self, cands: List[ClipCandidate], result: BatchResult):
        os.makedirs(self.out_dir, exist_ok=True)
        for cand in cands:
            cand.out_path = os.path.join(self.out_dir, f"reel_{uuid.uuid4().hex[:10]}.mp4")
            try:
                with gpu_job("render"):
                    cand.render_info = self.render_fn(
                        cand.segment["path"], cand.start, cand.end, cand.words, cand.source.source_handle,
                        cand.out_path,
                        bgm_path=(pick_bg_music(cand.source.niche, cand.source.music_slug)
                                  if settings.BG_MUSIC_ENABLED else None),
                        interpolate_fn=self.interpolate_fn, work_dir=settings.WORK_DIR)
                cand.qc = self.qc_fn(cand.out_path, cand.render_info.get("duration"))
            except Exception as e:
                log.exception("render failed")
                result.failed.append(cand)
                self.on_clip_failed(cand, f"render: {e}")
                continue
            if not cand.qc.passed:
                result.failed.append(cand)
                self.on_clip_failed(cand, "qc: " + "; ".join(cand.qc.reasons))
                _remove(cand.out_path)
                continue
            try:
                cand.extras = self.post_render(cand) or {}
            except Exception as e:
                log.warning("post_render hook failed: %s", e)
            result.produced.append(cand)
            self.on_clip_ready(cand)

    def run(self, sources: List[SourceVideo]) -> BatchResult:
        result = BatchResult()
        t = time.time()
        ready = self.stage_download(sources, result)
        result.stage_seconds["download"] = time.time() - t
        if not ready:
            return result
        t = time.time()
        ready = self.stage_whisper(ready, result)
        result.stage_seconds["whisper"] = time.time() - t
        t = time.time()
        cands = self.stage_qwen(ready) if ready else []
        result.stage_seconds["qwen"] = time.time() - t
        t = time.time()
        self.stage_render(cands, result)
        result.stage_seconds["render"] = time.time() - t
        log.info("batch done: produced=%d failed=%d source_errors=%d blocked=%s stages=%s",
                 len(result.produced), len(result.failed), len(result.source_errors), result.blocked,
                 {k: round(v) for k, v in result.stage_seconds.items()})
        return result


def seg_words(cand: ClipCandidate) -> list:
    return cand.segment.get("words") or []


def _remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
