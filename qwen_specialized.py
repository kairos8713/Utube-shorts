"""
qwen_specialized.py — the two content tasks left for Qwen:

  score_clips()   — find ~20–28 s viral moments in a segment transcript (final bounds: clip_bounds.py)
  rank_clips()    — rank ALL candidates of one video against each other, 1–100 each, no ties
  write_caption() — 1–2 sentence comment + 3–5 niche hashtags

The hook / insight / infobox / outro / YouTube-title prompts were removed with
the overlays they fed. Captions are assembled by code (credit line added here,
never by the model) and checked for links, @mentions, follow-CTAs and
similarity to previous captions.
"""
import difflib
import re
from typing import List, Optional

from clip_bounds import MAX_CLIP_S, MIN_CLIP_S
from llm_client import LLMBadOutput, get_llm
from log_setup import get_logger
from media_utils import parse_timecode
from prompt_safety import data_block, system_prompt

log = get_logger("qwen")

RAW_LENGTH_TOL_S = 3.0      # Qwen's raw times may be off by a few s; clip_bounds enforces 18–32 s
MIN_VIDEO_SCORE = 60           # 1–100 scale (was 7 on 0–9; every clip scored 9 in the full test)
CAPTION_SIMILARITY_MAX = 0.75
IG_CAPTION_MAX = 2200

URL_RE = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|net|org|io|ly|me|tv|gg)\b)", re.I)
MENTION_RE = re.compile(r"(^|\s)@\w")
FOLLOW_RE = re.compile(r"\b(follow|subscribe|link in (my )?bio|dm me|check out my)\b", re.I)
HASHTAG_RE = re.compile(r"^#[A-Za-z][A-Za-z0-9_]{1,29}$")


def _to_seconds(v) -> Optional[float]:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v.strip():
        return parse_timecode(v)
    return None


# ═════════════════════════════════════════════════════════════════════════════
# clip scoring
# ═════════════════════════════════════════════════════════════════════════════

_SCORE_SPEC = """{"clips": [{"start": "mm:ss", "end": "mm:ss", "score": 1-100 integer,
  "topic": "<3-6 words>", "reason": "<max 20 words>", "time_sensitive": true|false}]}
Up to 4 clips, each about 20-28 seconds long, not overlapping. Start where a sentence starts and
end where a sentence ends. No two clips may have the same score."""

_SCORE_ROLE = """You select the most engaging 20-28 second moments from a long-form video
transcript for Instagram Reels. Score 1-100 and use the whole range: below 60 boring/filler/intro
(do not return), 60-74 interesting, 75-89 surprising or high-energy, 90-100 only for a top 1% moment
(shocking, hilarious, unforgettable). Different clips must get different scores.
A clip must make sense on its own without the rest of the video: begin at the start of a
sentence and stop after a complete sentence, never mid-sentence. Mark time_sensitive=true
only for news / current events that lose value within days."""


def validate_scores(obj: dict) -> List[str]:
    errs = []
    clips = obj.get("clips")
    if not isinstance(clips, list):
        return ["'clips' must be a list"]
    for i, c in enumerate(clips[:8]):
        if not isinstance(c, dict):
            errs.append(f"clip {i} is not an object")
            continue
        if _to_seconds(c.get("start")) is None or _to_seconds(c.get("end")) is None:
            errs.append(f"clip {i} missing start/end")
        sc = c.get("score")
        if not isinstance(sc, int) or not 1 <= sc <= 100:
            errs.append(f"clip {i} score must be an integer 1-100")
    scores = [c.get("score") for c in clips[:8] if isinstance(c, dict)]
    if len(scores) != len(set(scores)):
        errs.append("every clip needs a different score")
    return errs


def score_clips(segment_transcript: str, niche: str, segment_duration: float,
                performance_summary: str = "", llm=None) -> List[dict]:
    """Returns validated clips with start/end in SEGMENT seconds, sorted by score desc."""
    llm = llm or get_llm()
    user = "\n\n".join(filter(None, [
        f"NICHE: {niche}",
        data_block("PAST_PERFORMANCE", performance_summary) if performance_summary else "",
        data_block("TRANSCRIPT", segment_transcript),
        "Return the best clips as JSON.",
    ]))
    msgs = [{"role": "system", "content": system_prompt(_SCORE_ROLE, _SCORE_SPEC)},
            {"role": "user", "content": user}]
    try:
        obj = llm.chat_json(msgs, task="clip_scoring", validator=validate_scores, max_tokens=900)
    except LLMBadOutput as e:
        log.warning("clip scoring failed: %s", e)
        return []
    # overlaps are resolved by the pipeline AFTER sentence alignment (a rejected best clip must not take
    # its overlapping alternatives with it)
    return filter_clips(obj.get("clips", []), segment_duration, drop_overlaps=False)


def filter_clips(raw: list, segment_duration: float, min_score: int = MIN_VIDEO_SCORE,
                 drop_overlaps: bool = True) -> List[dict]:
    """Code-side rules: raw length 18–32 s (± tolerance, aligned later), inside the segment,
    score ≥ MIN_VIDEO_SCORE, no overlaps."""
    out = []
    for c in sorted(raw, key=lambda c: -(c.get("score") or 0)):
        start, end = _to_seconds(c.get("start")), _to_seconds(c.get("end"))
        if start is None or end is None:
            continue
        dur = end - start
        if not (MIN_CLIP_S - RAW_LENGTH_TOL_S <= dur <= MAX_CLIP_S + RAW_LENGTH_TOL_S):
            continue
        if start < 0 or (segment_duration and end > segment_duration + 0.5):
            continue
        if (c.get("score") or 0) < min_score:
            continue
        if drop_overlaps and any(start < o["end"] and o["start"] < end for o in out):
            continue
        out.append({"start": start, "end": min(end, segment_duration or end), "score": int(c["score"]),
                    "topic": str(c.get("topic") or "")[:80], "reason": str(c.get("reason") or "")[:200],
                    "time_sensitive": bool(c.get("time_sensitive"))})
    return out


_RANK_ROLE = """You rank candidate Instagram Reels clips cut from ONE long-form video against each
other: which would hold a viewer best on its own? Order ALL candidates from best to worst and give each
a score 1-100 that reflects the ranking: strictly decreasing, no two scores equal, and spread out (the
best 85-100, a clearly weaker one at least 10 points lower; a merely interesting one 60-74)."""
_RANK_SPEC = '{"ranking": [{"id": "<candidate id>", "score": <1-100>}, ...]}   // every id exactly once, best first'


def validate_ranking(obj: dict, ids: List[str]) -> List[str]:
    r = obj.get("ranking")
    if not isinstance(r, list) or not all(isinstance(x, dict) for x in r):
        return ["'ranking' must be a list of objects"]
    got = [str(x.get("id")) for x in r]
    errs = []
    if sorted(got) != sorted(ids):
        errs.append(f"rank every id exactly once: {ids}")
    scores = [x.get("score") for x in r]
    if not all(isinstance(v, int) and 1 <= v <= 100 for v in scores):
        errs.append("scores must be integers 1-100")
    elif any(b >= a for a, b in zip(scores, scores[1:])):
        errs.append("scores must be strictly decreasing from best to worst (no ties)")
    return errs


def rank_clips(candidates: List[dict], niche: str, llm=None) -> Optional[List[dict]]:
    """candidates: [{"id", "topic", "reason", "text"}] → [{"id", "score"}] best first, or None (keep the
    per-segment scores) when the model fails."""
    llm = llm or get_llm()
    ids = [str(c["id"]) for c in candidates]
    listing = "\n\n".join(f"[{c['id']}] {c.get('topic') or ''} — {c.get('reason') or ''}\n{(c.get('text') or '')[:600]}"
                            for c in candidates)
    msgs = [{"role": "system", "content": system_prompt(_RANK_ROLE, _RANK_SPEC)},
            {"role": "user", "content": f"NICHE: {niche}\n\n" + data_block("CANDIDATES", listing, 9000)
             + "\nRank all candidates."}]
    try:
        obj = llm.chat_json(msgs, task="clip_ranking", validator=lambda o: validate_ranking(o, ids), max_tokens=400)
    except LLMBadOutput as e:
        log.warning("clip ranking failed, keeping segment scores: %s", e)
        return None
    return [{"id": str(x["id"]), "score": int(x["score"])} for x in obj["ranking"]]


# ═════════════════════════════════════════════════════════════════════════════
# captions
# ═════════════════════════════════════════════════════════════════════════════

_CAPTION_SPEC = """{"comment": "<1-2 sentences, max 220 characters>",
 "hashtags": ["#tag1", "#tag2", "#tag3"]}   // 3 to 5 niche hashtags"""

_CAPTION_ROLE = """You write Instagram Reels captions for short clips cut from long videos.
The comment is 1-2 natural sentences: a sharp observation or question about what is said in
the clip. Only use facts present in the clip transcript. No links, no @mentions, no
'follow/subscribe' calls, no hashtags inside the comment, no emojis spam (max 1).
Hashtags: 3-5 specific to the niche and topic (no #shorts, #fyp, #viral)."""

_BANNED_TAGS = {"#shorts", "#fyp", "#viral", "#foryou", "#foryoupage", "#youtube"}


def validate_caption(obj: dict) -> List[str]:
    errs = []
    comment = obj.get("comment")
    if not isinstance(comment, str) or not (15 <= len(comment.strip()) <= 300):
        errs.append("comment must be a string of 15-300 characters")
    else:
        sentences = [s for s in re.split(r"[.!?]+(?:\s|$)", comment.strip()) if s.strip()]
        if len(sentences) > 2:
            errs.append("comment must be at most 2 sentences")
        if URL_RE.search(comment):
            errs.append("no links allowed")
        if "@" in comment:
            errs.append("no @ allowed (Instagram turns @name into a tag and notifies that user)")
        if FOLLOW_RE.search(comment):
            errs.append("no follow/subscribe calls")
        if "#" in comment:
            errs.append("no hashtags inside the comment")
    tags = obj.get("hashtags")
    if not isinstance(tags, list) or not (3 <= len(tags) <= 5):
        errs.append("hashtags must be a list of 3-5 items")
    else:
        for t in tags:
            if isinstance(t, str) and "@" in t:
                errs.append(f"no @ in hashtags: {t!r}")
            elif not isinstance(t, str) or not HASHTAG_RE.match(t):
                errs.append(f"bad hashtag {t!r}")
            elif t.lower() in _BANNED_TAGS:
                errs.append(f"hashtag {t} not allowed")
        if len({str(t).lower() for t in tags}) != len(tags):
            errs.append("duplicate hashtags")
    return errs


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower())


def caption_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def too_similar(comment: str, previous: List[str], threshold: float = CAPTION_SIMILARITY_MAX) -> bool:
    return any(caption_similarity(comment, p) >= threshold for p in previous or [])


def credit_line(source_handle: str, source_name: str = "") -> str:
    """"🎥 <channel display name> on YouTube" — never with "@": on Instagram "@x" becomes a clickable tag that
    notifies whoever owns x there (often not the YouTube creator). No name → the handle without "@"."""
    name = (source_name or "").replace("@", "").strip()
    handle = re.sub(r"[^A-Za-z0-9._]", "", (source_handle or "").replace("@", ""))
    return f"🎥 {name or handle or 'original creator'} on YouTube"


def compose_caption(comment: str, hashtags: List[str], source_handle: str, source_name: str = "") -> str:
    caption = f"{comment.strip()}\n\n{' '.join(hashtags)}\n{credit_line(source_handle, source_name)}"
    assert "@" not in caption, "caption must not contain @"     # validate_caption already rejects it
    return caption[:IG_CAPTION_MAX]


def write_caption(clip_transcript: str, niche: str, source_handle: str,
                  previous_captions: List[str] = None, source_name: str = "", llm=None,
                  max_rounds: int = 3) -> Optional[dict]:
    """Returns {'caption', 'comment', 'hashtags'} or None if no acceptable caption."""
    llm = llm or get_llm()
    previous = [p for p in (previous_captions or []) if p][-50:]
    avoid = ""
    for _ in range(max_rounds):
        user = "\n\n".join(filter(None, [
            f"NICHE: {niche}",
            data_block("CLIP_TRANSCRIPT", clip_transcript, 4000),
            data_block("RECENT_CAPTIONS_DO_NOT_REPEAT", "\n".join(previous[-8:]), 3000) if previous else "",
            avoid,
            "Write the caption JSON.",
        ]))
        msgs = [{"role": "system", "content": system_prompt(_CAPTION_ROLE, _CAPTION_SPEC)},
                {"role": "user", "content": user}]
        try:
            obj = llm.chat_json(msgs, task="caption", validator=validate_caption, max_tokens=300,
                                temperature=0.8)
        except LLMBadOutput as e:
            log.warning("caption failed: %s", e)
            return None
        comment = obj["comment"].strip()
        if too_similar(comment, previous):
            avoid = "Your last comment was too similar to a recent caption. Write a clearly different one."
            continue
        return {"comment": comment, "hashtags": obj["hashtags"],
                "caption": compose_caption(comment, obj["hashtags"], source_handle, source_name)}
    return None
