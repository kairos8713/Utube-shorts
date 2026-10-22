"""
source_rules.py — code-side rules for which YouTube channels may be clip sources (Emre, 2026-10-23).

Used by discovery (sources_manager) and by scripts/review_sources.py (read-only report on the real DB).

  * excluded publishers: big rights holders whose licence forbids derivatives or who are not suited for
    clipping — TED / TEDx / TED-Ed (CC BY-NC-ND), news & TV channels, film/series studios, music labels.
    Editable in config: SOURCE_EXCLUDE_CHANNEL_IDS (comma-separated) and SOURCE_EXCLUDE_NAME_PATTERNS
    (regexes separated by ";;", matched against channel title and handle, case-insensitive).
  * music channels: YouTube category "Music" on the sample video.
  * reaction / compilation format: ≥ 30 % of the last 15 videos mention react / reaction / compilation /
    TikTok / best of in title or description.
  * clip permission: explicit text in the channel description only; no signal → None ("unknown"), never a
    made-up constant.
"""
import re
from typing import List, Optional, Tuple

from config import settings

REACTION_SHARE_MAX = 0.30
RECENT_VIDEOS = 15
REACTION_RE = re.compile(r"\b(react|reacts|reacting|reaction|reactions|compilation|compilations|tik ?toks?|"
                         r"best of)\b", re.I)
MUSIC_CATEGORIES = {"music"}

# explicit permission signals in a channel description (code wins over the model)
_ALLOWED_RE = re.compile(r"(clips? (are |is )?(welcome|allowed|encouraged)|feel free to (clip|use|share|repost)|"
                         r"clipping (is )?(allowed|welcome|encouraged)|clippers? (are )?welcome|"
                         r"you (may|can) (clip|use|repost) (our|my|these|this)|"
                         r"creative commons(?! attribution[- ]non ?commercial[- ]no ?deriv)|\bcc[- ]by(?![- ]nc)(?![- ]nd)\b)",
                         re.I)
_FORBIDDEN_RE = re.compile(r"(do not (re-?upload|repost|reproduce|redistribute)|don'?t (re-?upload|repost)|"
                           r"no re-?uploads?|re-?uploads? (are |is )?(not allowed|prohibited|forbidden)|"
                           r"unauthori[sz]ed (use|reproduction|re-?upload)|all rights reserved|"
                           r"\bcc[- ]by[- ]nc[- ]nd\b|no ?deriv(ative)?s?)", re.I)
PERMISSION_ALLOWED, PERMISSION_FORBIDDEN = 0.9, 0.0

_GENERIC_NAME_WORDS = {"the", "podcast", "podcasts", "show", "official", "channel", "tv", "clips", "with",
                       "radio", "network", "media", "highlights", "pod", "and", "&", "-", "|"}


def _patterns() -> List[re.Pattern]:
    raw = settings.SOURCE_EXCLUDE_NAME_PATTERNS or ""
    return [re.compile(p.strip(), re.I) for p in raw.split(";;") if p.strip()]


def _channel_ids() -> set:
    return {x.strip() for x in (settings.SOURCE_EXCLUDE_CHANNEL_IDS or "").split(",") if x.strip()}


def excluded_publisher(title: str, handle: str = "", channel_id: str = "") -> Optional[str]:
    """Reason string when the channel is on the exclusion list, else None."""
    if channel_id and channel_id in _channel_ids():
        return f"excluded publisher (channel id {channel_id})"
    for name in (title or "", handle or ""):
        for pat in _patterns():
            m = pat.search(name)
            if m:
                return f"excluded publisher ('{m.group(0).strip()}' in '{name}')"
    return None


def reaction_share(videos: List[Tuple[str, str]]) -> float:
    """Share of the last 15 videos (title, description) that look like reaction / compilation content."""
    vids = [(t or "", d or "") for t, d in videos[:RECENT_VIDEOS]]
    if not vids:
        return 0.0
    return round(sum(1 for t, d in vids if REACTION_RE.search(f"{t}\n{d}")) / len(vids), 2)


def is_music(categories) -> bool:
    return any((c or "").strip().lower() in MUSIC_CATEGORIES for c in (categories or []))


def code_exclusion(title: str, handle: str, channel_id: str, videos: List[Tuple[str, str]],
                   categories=None) -> Optional[str]:
    """First matching code rule (publisher list, music category, reaction format) or None."""
    why = excluded_publisher(title, handle, channel_id)
    if why:
        return why
    if is_music(categories):
        return "music channel (YouTube category Music)"
    share = reaction_share(videos)
    if share >= REACTION_SHARE_MAX:
        return f"reaction/compilation format ({share:.0%} of the last {min(len(videos), RECENT_VIDEOS)} videos)"
    return None


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("’", "'")).strip().lower()


def permission_from_text(description: str) -> Tuple[Optional[float], str]:
    """(value, evidence) from explicit wording in the description; (None, "") when nothing is said."""
    d = description or ""
    m = _FORBIDDEN_RE.search(d)
    if m:
        return PERMISSION_FORBIDDEN, m.group(0)
    m = _ALLOWED_RE.search(d)
    if m:
        return PERMISSION_ALLOWED, m.group(0)
    return None, ""


def evidence_in_text(quote: str, description: str) -> bool:
    q = _norm_text(quote)
    return len(q) >= 6 and q in _norm_text(description)


def normalized_name(title: str) -> str:
    """'The Money Guy Show' → 'money guy'; used for the saturation searches."""
    words = re.findall(r"[a-z0-9']+", _norm_text(title))
    kept = [w for w in words if w not in _GENERIC_NAME_WORDS]
    return " ".join(kept or words)


def mentions_name(text: str, norm_name: str) -> bool:
    toks = norm_name.split()
    words = set(re.findall(r"[a-z0-9']+", _norm_text(text)))
    return bool(toks) and all(t in words for t in toks)
