"""
transcriber.py — Whisper transcription (segment + word level).

WhisperSession loads the model once for a whole batch (Phase-3 batch order:
all downloads → all Whisper → all Qwen → render), then frees VRAM.
The caller holds the GPU lock.
"""
import gc
from typing import List

from config import settings
from log_setup import get_logger
from media_utils import cleanup_gpu, seconds_to_timecode

log = get_logger("transcriber")


class WhisperSession:
    def __init__(self, model_name: str = None, device: str = None):
        self.model_name = model_name or settings.WHISPER_MODEL
        self.device = device
        self.model = None

    def __enter__(self):
        import torch
        import whisper
        dev = self.device or ("cuda:0" if torch.cuda.is_available() else "cpu")
        self.fp16 = dev.startswith("cuda")
        log.info("loading whisper %s on %s", self.model_name, dev)
        self.model = whisper.load_model(self.model_name, device=dev)
        return self

    def __exit__(self, *exc):
        self.model = None
        gc.collect()
        cleanup_gpu()
        log.info("whisper unloaded")
        return False

    def transcribe(self, path: str, words: bool = False, language: str = None) -> dict:
        kw = dict(fp16=self.fp16, beam_size=1, best_of=1)
        if language:
            kw["language"] = language
        if words:
            kw["word_timestamps"] = True
        return self.model.transcribe(path, **kw)

    def segment_transcript(self, path: str, language: str = "en") -> dict:
        """Returns {'text': '[mm:ss - mm:ss] ...' lines, 'segments': [...], 'language': str}."""
        res = self.transcribe(path, language=language)
        lines = [f"[{seconds_to_timecode(s['start'])} - {seconds_to_timecode(s['end'])}] {s['text'].strip()}"
                 for s in res.get("segments", [])]
        return {"text": "\n".join(lines), "segments": res.get("segments", []),
                "language": res.get("language", language)}

    def words(self, audio_path: str, language: str = "en") -> List[dict]:
        res = self.transcribe(audio_path, words=True, language=language)
        out = []
        for seg in res.get("segments", []):
            for w in seg.get("words", []) or []:
                out.append({"start": float(w.get("start", 0)), "end": float(w.get("end", 0)),
                            "text": (w.get("word") or "").strip()})
        return out

    def detect_language(self, path: str) -> str:
        res = self.transcribe(path)
        return res.get("language", "")


def speech_ratio(segments: list, duration: float) -> float:
    """Fraction of `duration` covered by Whisper speech segments (0..1)."""
    if duration <= 0:
        return 0.0
    covered = sum(max(0.0, float(s.get("end", 0)) - float(s.get("start", 0)))
                  for s in segments or [] if (s.get("text") or "").strip())
    return max(0.0, min(1.0, covered / duration))
