"""
sources_manager.py — fully automatic source pool (Emre's decision: no approval step).

Per cycle (cycle_maintenance):
  * discovery per niche: Qwen writes search queries → `yt-dlp ytsearch50:<q>
    --flat-playlist` → candidate channels → stats (subscribers, avg views,
    upload rate, durations, description, recent titles)
  * exclusion (source_rules.py, before any model call): editable publisher list (TED/TEDx, news/TV,
    studios, labels), YouTube category Music, reaction/compilation format (≥ 30 % of the last 15 videos)
  * scoring: Qwen judges niche fit (last 15 titles + descriptions), format suitability and quotes any
    clip-permission wording (kept only if the quote is really in the description; no wording → permission
    unknown, left out of the score); code measures speech ratio + language (Whisper on a 3-min audio
    sample) and saturation (distinct channels posting "<name> clips / podcast clips / shorts");
    score ≥ threshold → TRIAL
  * trial evaluation: 3–5 trial clips on different accounts; 72 h after the
    last one is published, median relative views ≥ 0.8 → ACTIVE else REJECTED
  * bandit update (Beta posterior per source from relative performance)
  * pool health: < 5 ACTIVE sources in a niche → discovery doubles + warning
plan_production(): Thompson sampling over ACTIVE sources (+ trial sources that
still need clips + a guaranteed share for little-tried sources).
fast_lane(): every 6 h, new uploads of strong sources → 1–3 priority clips.
"""
import datetime as _dt
import json
import math
import random
import re
import statistics
from collections import defaultdict
from typing import List, Optional

import downloader
import source_rules
from alerts import raise_alert
from analytics import relative
from brain.core import log_decision
from credit import handle_from_url
from llm_client import LLMError, get_llm
from log_setup import get_logger
from models import OwnedChannels, Post, ProcessedSourceVideos, Sectors, Source, SourceTrial, videos
from production import SourceVideo
from prompt_safety import data_block, system_prompt
from timeutil import utcnow

log = get_logger("sources")

SCORE_THRESHOLD = 0.55
MIN_ACTIVE_PER_NICHE = 5
TRIAL_CLIPS = 4                 # 3–5 per task; 4 spread over accounts
TRIAL_PASS_MEDIAN = 0.8
TRIAL_WAIT = _dt.timedelta(hours=72)
MIN_AVG_DURATION_S = 8 * 60
CLIPS_PER_VIDEO = 3
EXPLORE_SOURCE_SHARE = 0.15     # share of picks reserved for the least-tried active sources
FAST_LANE_MIN_SCORE = 0.7
BANDIT_SUCCESS_REL = 1.0


# ═════════════════════════════════════════════════════════════════════════════
# YouTube access (injectable)
# ═════════════════════════════════════════════════════════════════════════════

class YouTubeAPI:
    def search(self, query: str, n: int = 50) -> list:
        return downloader.ytdlp_json(f"ytsearch{n}:{query}", ["--flat-playlist"], timeout=180).get("entries") or []

    def channel_videos(self, channel_url: str, n: int = 15) -> dict:
        url = channel_url.rstrip("/") + "/videos"
        return downloader.ytdlp_json(url, ["--flat-playlist", "--playlist-end", str(n)], timeout=180)

    def video_categories(self, video_url: str) -> list:
        """YouTube categories of one video (metadata only, no download)."""
        return downloader.ytdlp_json(video_url, ["--skip-download"], timeout=180).get("categories") or []

    def speech_sample(self, video_url: str):
        """(speech_ratio, language) from a 3-min audio sample (holds the GPU lock)."""
        from gpu_lock import gpu_job
        from transcriber import WhisperSession, speech_ratio
        path = downloader.download_audio_sample(video_url, 60, 240)
        with gpu_job("whisper_sample"):
            with WhisperSession() as ws:
                res = ws.transcribe(path)
        return speech_ratio(res.get("segments", []), 180.0), res.get("language", "")

    def delay(self):
        downloader.polite_delay(3, 12)


# ═════════════════════════════════════════════════════════════════════════════
# helpers
# ═════════════════════════════════════════════════════════════════════════════

def niches(session):
    return session.query(Sectors).all()


def active_count(session, sector_id) -> int:
    return session.query(Source).filter_by(sector_id=sector_id, status="ACTIVE").count()


def blacklist_source(session, source_id: Optional[int], reason: str):
    if not source_id:
        return
    src = session.get(Source, source_id)
    if src and src.status != "BLACKLISTED":
        src.status, src.status_reason, src.status_changed_at = "BLACKLISTED", reason[:300], utcnow()
        log_decision(session, "source_blacklist", {"source_id": src.id}, "OK", True, reason)
        session.commit()
        log.warning("source %s blacklisted: %s", src.channel_url, reason)


def entry_published_at(e: dict) -> Optional[_dt.datetime]:
    """Publish date of a flat-playlist entry (timestamp or upload_date YYYYMMDD), UTC naive."""
    ts = e.get("timestamp") or e.get("release_timestamp")
    if ts:
        return _dt.datetime.utcfromtimestamp(ts)
    d = e.get("upload_date")
    if d and len(str(d)) == 8:
        try:
            return _dt.datetime.strptime(str(d), "%Y%m%d")
        except ValueError:
            return None
    return None


def _channel_key(e: dict) -> Optional[str]:
    url = e.get("channel_url") or e.get("uploader_url")
    if not url and e.get("channel_id"):
        url = f"https://www.youtube.com/channel/{e['channel_id']}"
    if not url:
        return None
    url = url.rstrip("/")
    m = re.match(r"^https://www\.youtube\.com/(@[\w.-]+|channel/[\w-]+)$", url)
    return url if m else None


def meta_handle(meta: dict) -> str:
    """'@Some.Handle.' from yt-dlp channel metadata (uploader_id / uploader_url), without the '@'.
    Search results only carry /channel/UC... URLs, so the handle cannot come from the URL."""
    for v in (meta.get("uploader_id"), meta.get("uploader_url")):
        h = handle_from_url(v) if v and "/@" in str(v) else (str(v)[1:] if v and str(v).startswith("@") else "")
        if h:
            return h
    return ""


def backfill_handle(src, meta: dict):
    if not src.handle:
        h = meta_handle(meta)
        if h:
            src.handle = h
            log.info("source %s: handle @%s from channel metadata", src.id, h)


def channel_stats(meta: dict) -> dict:
    entries = [e for e in (meta.get("entries") or []) if e]
    durs = [e["duration"] for e in entries if e.get("duration")]
    views = [e["view_count"] for e in entries if e.get("view_count") is not None]
    dates = sorted(e.get("timestamp") or 0 for e in entries if e.get("timestamp"))
    per_week = None
    if len(dates) >= 2 and dates[-1] > dates[0]:
        per_week = round((len(dates) - 1) / ((dates[-1] - dates[0]) / 604800.0), 2)
    return {
        "title": meta.get("channel") or meta.get("uploader") or meta.get("title"),
        "handle": meta_handle(meta),
        "yt_channel_id": meta.get("channel_id"),
        "subscribers": meta.get("channel_follower_count"),
        "description": (meta.get("description") or "")[:2000],
        "recent_titles": [e.get("title") for e in entries[:source_rules.RECENT_VIDEOS] if e.get("title")],
        "recent_videos": [(e.get("title") or "", (e.get("description") or "")[:300])
                          for e in entries[:source_rules.RECENT_VIDEOS]],
        "avg_duration_s": statistics.mean(durs) if durs else None,
        "avg_views": statistics.mean(views) if views else None,
        "uploads_per_week": per_week,
        "sample_video": next((f"https://www.youtube.com/watch?v={e['id']}" for e in entries
                              if e.get("id") and (e.get("duration") or 0) >= MIN_AVG_DURATION_S), None),
    }


# ── Qwen tasks (narrow) ──────────────────────────────────────────────────────

_Q_ROLE = """You find YouTube channels whose long-form videos (podcasts, interviews, talks,
commentary) can be cut into engaging 20-30 s Instagram Reels for the given niche. Write search
queries that surface such channels (English-language content)."""
_Q_SPEC = '{"queries": ["<2-5 search queries, max 80 chars each>"]}'


def _v_queries(o):
    q = o.get("queries")
    if not isinstance(q, list) or not (1 <= len(q) <= 5) or not all(isinstance(x, str) and 3 <= len(x) <= 80 for x in q):
        return ["queries must be a list of 1-5 strings (3-80 chars)"]
    return []


def search_queries(session, niche: str, n: int, llm=None) -> list:
    llm = llm or get_llm()
    msgs = [{"role": "system", "content": system_prompt(_Q_ROLE, _Q_SPEC)},
            {"role": "user", "content": f"NICHE: {niche}\nWrite {n} queries."}]
    try:
        obj = llm.chat_json(msgs, task="source_queries", validator=_v_queries, max_tokens=200)
    except LLMError as e:
        log.warning("query generation failed: %s", e)
        return [f"{niche} podcast full episode"]
    log_decision(session, "source_queries", obj, "OK", True, input_summary=niche)
    return obj["queries"][:n]


_A_ROLE = """You assess whether a YouTube channel is a good SOURCE of clips for an Instagram
Reels page in the given niche. Use the channel description and its last 15 videos (titles +
descriptions).
niche_fit: 0-1 how well its videos fit the niche (a music channel, a gaming channel or a news desk is
a poor fit for a psychology or finance niche even if one title sounds related).
format: "ok" for original long-form talk (podcasts, interviews, talks, commentary), else one of
"music", "reaction_compilation" (reacts to / compiles other people's videos or TikToks), "news_tv",
"studio" (film/series studio), "music_label", "reupload" (posts other creators' content), "other".
clip_permission: "allowed" or "forbidden" ONLY if the description says so explicitly (clips welcome,
feel free to use, creative commons / no re-uploads, all rights reserved...), else "unknown".
clip_permission_evidence: the exact words from the description that say it, or "" when unknown."""
_A_SPEC = ('{"niche_fit": <0-1>, "format": "ok|music|reaction_compilation|news_tv|studio|music_label|reupload|other", '
           '"clip_permission": "allowed|forbidden|unknown", "clip_permission_evidence": "<exact quote or empty>", '
           '"reason": "<max 25 words>"}')
FORMATS = ("ok", "music", "reaction_compilation", "news_tv", "studio", "music_label", "reupload", "other")


def _v_assess(o):
    errs = []
    if not isinstance(o.get("niche_fit"), (int, float)) or not 0 <= o["niche_fit"] <= 1:
        errs.append("niche_fit must be a number 0-1")
    if o.get("format") not in FORMATS:
        errs.append(f"format must be one of {FORMATS}")
    if o.get("clip_permission") not in ("allowed", "forbidden", "unknown"):
        errs.append("clip_permission must be allowed|forbidden|unknown")
    if not isinstance(o.get("clip_permission_evidence", ""), str):
        errs.append("clip_permission_evidence must be a string")
    return errs


def _videos_block(stats: dict) -> str:
    vids = stats.get("recent_videos") or [(t, "") for t in stats.get("recent_titles") or []]
    return "\n".join(f"- {t}" + (f" — {d}" if d else "") for t, d in vids[:source_rules.RECENT_VIDEOS])


def assess(session, niche: str, stats: dict, llm=None) -> dict:
    llm = llm or get_llm()
    user = "\n\n".join([f"NICHE: {niche}", data_block("CHANNEL_DESCRIPTION", stats.get("description") or "", 2000),
                        data_block("RECENT_VIDEOS", _videos_block(stats), 4000),
                        "Assess this channel."])
    msgs = [{"role": "system", "content": system_prompt(_A_ROLE, _A_SPEC)}, {"role": "user", "content": user}]
    try:
        return llm.chat_json(msgs, task="source_assessment", validator=_v_assess, max_tokens=220)
    except LLMError as e:
        log.warning("assessment failed: %s", e)
        return {"niche_fit": 0.0, "format": "ok", "clip_permission": "unknown", "clip_permission_evidence": "",
                "reason": f"LLM failed: {e}"}


def resolve_permission(description: str, assessment: dict):
    """0.9 / 0.0 from explicit description wording (code regex first, then the model's quote if it is
    really in the description); None = unknown (left out of the score)."""
    value, _ = source_rules.permission_from_text(description)
    if value is not None:
        return value
    verdict = assessment.get("clip_permission")
    if verdict in ("allowed", "forbidden") and \
            source_rules.evidence_in_text(assessment.get("clip_permission_evidence") or "", description):
        return source_rules.PERMISSION_ALLOWED if verdict == "allowed" else source_rules.PERMISSION_FORBIDDEN
    return None


# ── scoring ──────────────────────────────────────────────────────────────────

SATURATION_QUERIES = ("{} clips", "{} podcast clips", "{} shorts")


def saturation(yt, stats: dict, own_url: str):
    """(0-1, n): n = distinct OTHER channels whose result titles mention the normalized channel name in
    searches for "<name> clips / podcast clips / shorts"; 10+ channels → 1.0."""
    norm = source_rules.normalized_name(stats.get("title") or "")
    if not norm:
        return 0.0, 0
    others = set()
    for q in SATURATION_QUERIES:
        try:
            hits = yt.search(q.format(norm), 20)
        except downloader.DownloadError:
            return 0.5, -1
        for e in hits:
            key = _channel_key(e)
            if key and key != own_url and source_rules.mentions_name(e.get("title") or "", norm):
                others.add(key)
        yt.delay()
    return round(min(1.0, len(others) / 10.0), 2), len(others)


def score_candidate(stats: dict, assessment: dict, speech: float, sat: float, permission=None) -> float:
    """Weighted 0-1. An unknown clip permission (None) is left out and the other weights are rescaled."""
    views = stats.get("avg_views") or 0
    views_factor = min(1.0, math.log10(views + 1) / 6.0)      # 1M avg views → 1.0
    base = (0.35 * assessment.get("niche_fit", 0) + 0.25 * speech + 0.15 * (1 - sat) + 0.10 * views_factor)
    if permission is None:
        return round(base / 0.85, 3)
    return round(base + 0.15 * permission, 3)


def discover_niche(session, sector, yt=None, llm=None, intensify=False, max_candidates=None) -> dict:
    yt = yt or YouTubeAPI()
    n_q = 4 if intensify else 2
    max_c = max_candidates or (10 if intensify else 5)
    known = {s.channel_url for s in session.query(Source.channel_url).all()}
    counts = defaultdict(int)
    for q in search_queries(session, sector.name, n_q, llm):
        for e in yt.search(q, 50):
            key = _channel_key(e)
            if key and key not in known and (e.get("duration") or 0) >= MIN_AVG_DURATION_S / 2:
                counts[key] += 1
        yt.delay()
    out = {"candidates": 0, "trial": [], "rejected": 0}
    for url, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:max_c]:
        out["candidates"] += 1
        meta = yt.channel_videos(url, 15)
        stats = channel_stats(meta)
        yt.delay()
        reason = None
        if not stats["sample_video"] or (stats["avg_duration_s"] or 0) < MIN_AVG_DURATION_S:
            reason = "no long-form videos"
        a = {"niche_fit": 0, "format": "", "clip_permission": "unknown"}
        speech, lang, sat, sat_n, permission = 0.0, "", 1.0, 0, None
        if not reason:                                   # code rules first: no model call, no download
            reason = source_rules.excluded_publisher(stats["title"] or "", stats["handle"] or "",
                                                     stats["yt_channel_id"] or "")
        if not reason:
            cats = []
            if hasattr(yt, "video_categories"):
                try:
                    cats = yt.video_categories(stats["sample_video"])
                except downloader.YouTubeBlocked:
                    raise
                except downloader.DownloadError as e:
                    log.warning("categories of %s unknown: %s", stats["sample_video"], e)
            reason = source_rules.code_exclusion(stats["title"] or "", stats["handle"] or "",
                                                 stats["yt_channel_id"] or "", stats.get("recent_videos") or [],
                                                 cats)
        if not reason:
            a = assess(session, sector.name, stats, llm)
            if a.get("format", "ok") != "ok":
                reason = f"unsuitable format: {a['format']} (Qwen)"
            elif a["niche_fit"] < 0.4:
                reason = f"niche fit {a['niche_fit']}"
            permission = resolve_permission(stats["description"], a)
        if not reason:
            speech, lang = yt.speech_sample(stats["sample_video"])
            if lang and lang != "en":
                reason = f"language {lang}"
        if not reason:
            sat, sat_n = saturation(yt, stats, url)
        score = score_candidate(stats, a, speech, sat, permission) if not reason else 0.0
        status = "TRIAL" if not reason and score >= SCORE_THRESHOLD else "REJECTED"
        src = Source(channel_url=url, handle=stats["handle"] or handle_from_url(url), title=stats["title"],
                     sector_id=sector.id,
                     yt_channel_id=stats["yt_channel_id"], subscribers=stats["subscribers"],
                     avg_views=stats["avg_views"], uploads_per_week=stats["uploads_per_week"],
                     avg_duration_s=stats["avg_duration_s"], description=stats["description"],
                     recent_titles=json.dumps(stats["recent_titles"]), niche_fit=a.get("niche_fit"),
                     clip_permission=permission, speech_ratio=speech, language=lang,
                     saturation=sat, score=score, status=status,
                     status_reason=reason or (f"score {score} vs threshold {SCORE_THRESHOLD} (clip channels: "
                                              f"{sat_n}, permission: {'unknown' if permission is None else permission})"),
                     status_changed_at=utcnow(), discovered_at=utcnow())
        session.add(src)
        session.flush()
        log_decision(session, "source_add" if status == "TRIAL" else "source_remove",
                     {"source_id": src.id, "channel_url": url, "score": score, "status": status},
                     "OK", True, a.get("reason", "") if not reason else reason, input_summary=sector.name)
        if status == "TRIAL":
            out["trial"].append(url)
        else:
            out["rejected"] += 1
    session.commit()
    return out


# ── trials + bandit ──────────────────────────────────────────────────────────

def _source_posts(session, source_id):
    return session.query(Post).filter(Post.source_id == source_id, Post.status == "PUBLISHED").all()


def update_bandit(session, now=None):
    now = now or utcnow()
    cache = {}
    for src in session.query(Source).filter(Source.status.in_(("ACTIVE", "TRIAL"))).all():
        wins = losses = 0
        for p in _source_posts(session, src.id):
            r = relative(session, p, 24, now=now, _median_cache=cache)
            if r is None:
                continue
            if r >= BANDIT_SUCCESS_REL:
                wins += 1
            else:
                losses += 1
        src.bandit_alpha, src.bandit_beta = 1.0 + wins, 1.0 + losses
    session.commit()


def evaluate_trials(session, now=None) -> dict:
    now = now or utcnow()
    out = {"active": [], "rejected": []}
    for src in session.query(Source).filter_by(status="TRIAL").all():
        posts = sorted(_source_posts(session, src.id), key=lambda p: p.published_at)
        if len(posts) < 3:
            continue
        last = posts[-1].published_at
        if now - last < TRIAL_WAIT:
            continue
        rels = [r for r in (relative(session, p, 72, now=now) or relative(session, p, 24, now=now) for p in posts)
                if r is not None]
        if len(rels) < 3:
            continue
        med = statistics.median(rels)
        result = "PASS" if med >= TRIAL_PASS_MEDIAN else "FAIL"
        session.add(SourceTrial(source_id=src.id, started_at=posts[0].published_at, evaluate_after=last + TRIAL_WAIT,
                                evaluated_at=now, clips_target=TRIAL_CLIPS, median_relative=round(med, 3),
                                result=result, notes=f"{len(rels)} posts"))
        src.status = "ACTIVE" if result == "PASS" else "REJECTED"
        src.status_reason = f"trial median relative {med:.2f} (pass ≥ {TRIAL_PASS_MEDIAN})"
        src.status_changed_at = now
        log_decision(session, "source_trial_result", {"source_id": src.id, "median": med, "result": result},
                     "OK", True, src.status_reason)
        out["active" if result == "PASS" else "rejected"].append(src.id)
    session.commit()
    return out


def cycle_maintenance(session, now=None, llm=None, yt=None) -> dict:
    now = now or utcnow()
    yt = yt or YouTubeAPI()
    stats = {"trials": evaluate_trials(session, now)}
    update_bandit(session, now)
    disc = {}
    for sec in niches(session):
        n_active = active_count(session, sec.id)
        intensify = n_active < MIN_ACTIVE_PER_NICHE
        if intensify:
            raise_alert("SOURCE_POOL_LOW", f"Niche '{sec.name}' has only {n_active} active sources — discovery "
                        "intensified", severity="INFO", email=False, session=session)
        try:
            disc[sec.name] = discover_niche(session, sec, yt, llm, intensify)
        except downloader.YouTubeBlocked as e:
            raise_alert("YOUTUBE_BLOCKED", f"Source discovery stopped: {e}", severity="CRITICAL", session=session)
            disc[sec.name] = {"error": str(e)}
            break
        except downloader.DownloadError as e:
            disc[sec.name] = {"error": str(e)[:200]}
    stats["discovery"] = disc
    return stats


# ── production planning ──────────────────────────────────────────────────────

def _used_urls(session) -> set:
    a = {r[0] for r in session.query(ProcessedSourceVideos.video_url).all()}
    b = {r[0] for r in session.query(videos.source_url).filter(videos.source_url != None).all()}  # noqa: E711
    return a | b


def trial_needs_clips(session, src) -> bool:
    made = session.query(videos).filter_by(source_id=src.id).count()
    return made < TRIAL_CLIPS


def pick_sources(session, sector_id, n, rng=None) -> List[Source]:
    rng = rng or random.Random()
    picks = [s for s in session.query(Source).filter_by(sector_id=sector_id, status="TRIAL").all()
             if trial_needs_clips(session, s)][:n]
    active = session.query(Source).filter_by(sector_id=sector_id, status="ACTIVE").all()
    for _ in range(max(0, n - len(picks)) if active else 0):
        if rng.random() < EXPLORE_SOURCE_SHARE:          # regular share for little-tried sources
            choice = min(active, key=lambda s: (s.clips_published or 0, rng.random()))
        else:                                            # Thompson sampling
            choice = max(active, key=lambda s: rng.betavariate(s.bandit_alpha or 1, s.bandit_beta or 1))
        picks.append(choice)                             # a source may supply several videos
    return picks


def plan_production(session, needs: dict, now=None, yt=None, rng=None) -> List[SourceVideo]:
    yt = yt or YouTubeAPI()
    used = _used_urls(session)
    plan = []
    for sector_id, clips_needed in needs.items():
        sector = session.get(Sectors, sector_id)
        n_videos = math.ceil(clips_needed / CLIPS_PER_VIDEO) + 1
        sources = pick_sources(session, sector_id, n_videos, rng)
        if not sources:
            raise_alert("SOURCE_POOL_EMPTY", f"No ACTIVE/TRIAL sources for niche '{sector.name}' — nothing to "
                        "produce until discovery finds some", severity="WARNING", email=False, session=session)
            continue
        per_source = defaultdict(int)
        for s in sources:
            per_source[s.id] += 1
        for src in {s.id: s for s in sources}.values():
            try:
                meta = yt.channel_videos(src.channel_url, 30)
                backfill_handle(src, meta)
            except downloader.YouTubeBlocked:
                raise
            except downloader.DownloadError as e:
                log.warning("cannot list %s: %s", src.channel_url, e)
                continue
            fresh = [e for e in (meta.get("entries") or []) if e and e.get("id")
                     and (e.get("duration") or MIN_AVG_DURATION_S) >= MIN_AVG_DURATION_S
                     and f"https://www.youtube.com/watch?v={e['id']}" not in used]
            for e in fresh[:per_source[src.id]]:
                url = f"https://www.youtube.com/watch?v={e['id']}"
                used.add(url)
                max_clips = min(CLIPS_PER_VIDEO, TRIAL_CLIPS) if src.status == "TRIAL" else CLIPS_PER_VIDEO
                plan.append(SourceVideo(url=url, niche=sector.name, source_id=src.id,
                                        source_handle=src.handle or handle_from_url(src.channel_url),
                                        source_name=src.title or "", music_slug=sector.music_slug,
                                        max_clips=max_clips, title=e.get("title") or "",
                                        published_at=entry_published_at(e)))
                session.add(ProcessedSourceVideos(video_url=url, sector_id=sector_id, status="PLANNED",
                                                  processed_at=utcnow()))
            src.last_checked_at = utcnow()
    session.commit()
    log.info("production plan: %d source videos for needs %s", len(plan), needs)
    return plan


def performance_summary(session, src: SourceVideo) -> str:
    """Feedback for clip scoring: how earlier clips of this source / similar topics performed."""
    if not src.source_id:
        return ""
    lines, cache = [], {}
    rows = (session.query(Post, videos).join(videos, videos.id == Post.video_id)
            .filter(Post.source_id == src.source_id, Post.status == "PUBLISHED")
            .order_by(Post.published_at.desc()).limit(15).all())
    for p, v in rows:
        r = relative(session, p, 24, _median_cache=cache)
        if r is not None:
            lines.append(f"- topic '{v.topic or '?'}', {v.clip_duration or 0:.0f}s, qwen score {v.qwen_point}: "
                         f"{r:.2f}x the account's typical views")
    if not lines:
        return ""
    return "Earlier clips from this source (relative to account median; >1 = above typical):\n" + "\n".join(lines)


# ── fast lane ────────────────────────────────────────────────────────────────

def fast_lane(session, now=None, yt=None, pipeline_factory=None, llm=None) -> dict:
    """Every 6 h: new uploads of strong ACTIVE sources → 1–3 priority clips within 24 h."""
    from production import Pipeline
    from production_store import ProductionStore, assign_videos
    import scheduler
    now = now or utcnow()
    yt = yt or YouTubeAPI()
    used = _used_urls(session)
    plan = []
    for src in session.query(Source).filter_by(status="ACTIVE").all():
        strong = (src.score or 0) >= FAST_LANE_MIN_SCORE or \
            (src.bandit_alpha or 1) / ((src.bandit_alpha or 1) + (src.bandit_beta or 1)) >= 0.6
        try:
            meta = yt.channel_videos(src.channel_url, 3)
            backfill_handle(src, meta)
        except downloader.YouTubeBlocked as e:
            raise_alert("YOUTUBE_BLOCKED", f"Fast lane stopped: {e}", severity="CRITICAL", session=session)
            break
        except downloader.DownloadError:
            continue
        entries = [e for e in (meta.get("entries") or []) if e and e.get("id")]
        if not entries:
            continue
        newest = entries[0]["id"]
        first_seen = src.last_video_seen is None
        is_new = newest != src.last_video_seen
        src.last_video_seen = newest
        url = f"https://www.youtube.com/watch?v={newest}"
        if first_seen or not is_new or not strong or url in used:
            continue
        if (entries[0].get("duration") or MIN_AVG_DURATION_S) < MIN_AVG_DURATION_S:
            continue
        sec = session.get(Sectors, src.sector_id)
        plan.append(SourceVideo(url=url, niche=sec.name, source_id=src.id, source_handle=src.handle or "",
                                source_name=src.title or "", music_slug=sec.music_slug, max_clips=3, priority=1,
                                title=entries[0].get("title") or "", published_at=entry_published_at(entries[0])))
        session.add(ProcessedSourceVideos(video_url=url, sector_id=src.sector_id, status="FAST_LANE",
                                          processed_at=now))
    session.commit()
    if not plan:
        return {"videos": 0}
    store = ProductionStore(session, cycle_id=int(now.timestamp()))
    ready = []

    def on_ready(cand):
        v = store.on_clip_ready(cand)
        if v is not None:
            v.time_sensitive = True          # priority in queue ordering
            session.commit()
            ready.append(v)
    factory = pipeline_factory or Pipeline
    factory(llm=llm, is_duplicate=store.is_duplicate, previous_captions=store.previous_captions,
            post_render=store.post_render, on_clip_ready=on_ready, on_clip_failed=store.on_clip_failed,
            performance_summary=lambda s: performance_summary(session, s)).run(plan)
    assign_videos(session)
    touched = {v.owned_channel_id for v in ready if v.owned_channel_id}
    for acc_id in touched:
        scheduler.insert_priority(session, session.get(OwnedChannels, acc_id), now)
    log.info("fast lane: %d videos → %d clips on %d accounts", len(plan), len(ready), len(touched))
    return {"videos": len(plan), "clips": len(ready), "accounts": len(touched)}
