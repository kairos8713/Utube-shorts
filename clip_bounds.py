"""
clip_bounds.py — move Qwen's clip start/end onto sentence boundaries using Whisper word timestamps.

Qwen picks whole seconds from a segment transcript, so clips used to start and end mid-sentence
(full test 2026-10-22: 6/6). Rules (Emre, 2026-10-23):

  start → a sentence start: the previous word ends with . ? ! or there is ≥ 0.6 s of silence before
          the word. Search back ≤ 3 s from Qwen's start, else forward ≤ 2 s.
  end   → a sentence end: the word ends with . ? ! or ≥ 0.6 s of silence follows it.
          Search forward ≤ 4 s from Qwen's end, else back ≤ 3 s. + 0.25 s tail (never into the
          next word).
  A boundary within ±0.5 s of Qwen's time counts as "already there" (Qwen only has whole seconds).
  No boundary, or a final length outside 18–32 s → the clip is rejected (reason returned).

All times are segment seconds; words are [{"start", "end", "text"}] in that time base.
"""
from typing import List, Optional, Tuple

MIN_CLIP_S, MAX_CLIP_S = 18.0, 32.0
SILENCE_GAP_S = 0.6
START_BACK_S, START_FWD_S = 3.0, 2.0
END_FWD_S, END_BACK_S = 4.0, 3.0
END_TAIL_S = 0.25
SNAP_TOL_S = 0.5
_SENTENCE_END = (".", "?", "!", ".\"", "?\"", "!\"", ".”", "?”", "!”", "…")


def _ends_sentence(text: str) -> bool:
    return (text or "").strip().endswith(_SENTENCE_END)


def sentence_starts(words: List[dict]) -> List[int]:
    """Indices of words that begin a sentence."""
    out = []
    for i, w in enumerate(words):
        if i == 0:
            if w["start"] >= SILENCE_GAP_S:          # silence before the first word of the segment
                out.append(i)
            continue
        prev = words[i - 1]
        if _ends_sentence(prev["text"]) or w["start"] - prev["end"] >= SILENCE_GAP_S:
            out.append(i)
    return out


def sentence_ends(words: List[dict]) -> List[int]:
    """Indices of words that end a sentence (the last word only with punctuation)."""
    out = []
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else None
        if _ends_sentence(w["text"]) or (nxt is not None and nxt["start"] - w["end"] >= SILENCE_GAP_S):
            out.append(i)
    return out


def _pick(times: List[float], target: float, back: float, fwd: float, prefer: str) -> Optional[float]:
    """Nearest boundary within ±SNAP_TOL_S; else the closest one in the preferred direction, then the other."""
    near = [t for t in times if abs(t - target) <= SNAP_TOL_S]
    if near:
        return min(near, key=lambda t: abs(t - target))
    before = [t for t in times if target - back <= t < target]
    after = [t for t in times if target < t <= target + fwd]
    first, second = (before, after) if prefer == "back" else (after, before)
    for group in (first, second):
        if group:
            return min(group, key=lambda t: abs(t - target))
    return None


def align_clip(words: List[dict], start: float, end: float,
               segment_duration: float = None) -> Tuple[Optional[Tuple[float, float]], str]:
    """((start, end), "") on success, (None, reason) when the clip must be rejected."""
    words = [w for w in words if (w.get("text") or "").strip()]
    if not words:
        return None, "no word timestamps"
    starts = {words[i]["start"]: i for i in sentence_starts(words)}
    ends = {words[i]["end"]: i for i in sentence_ends(words)}
    s = _pick(sorted(starts), start, START_BACK_S, START_FWD_S, prefer="back")
    if s is None:
        return None, f"no sentence start within -{START_BACK_S:.0f}/+{START_FWD_S:.0f} s of {start:.1f}"
    e_word = _pick(sorted(ends), end, END_BACK_S, END_FWD_S, prefer="fwd")
    if e_word is None:
        return None, f"no sentence end within +{END_FWD_S:.0f}/-{END_BACK_S:.0f} s of {end:.1f}"
    j = ends[e_word]
    tail_limit = words[j + 1]["start"] if j + 1 < len(words) else e_word + END_TAIL_S
    e = min(e_word + END_TAIL_S, tail_limit)
    if segment_duration:
        e = min(e, segment_duration)
    dur = e - s
    if not (MIN_CLIP_S <= dur <= MAX_CLIP_S):
        return None, f"aligned length {dur:.1f} s outside {MIN_CLIP_S:.0f}-{MAX_CLIP_S:.0f} s"
    return (round(s, 3), round(e, 3)), ""
