"""
reports.py — end-of-cycle report. Code counts everything; Qwen only writes the
narrative + decision summary (fallback: a plain template).

Sections (panel order): Dikkat gerektirenler → özet → hesaplar → Qwen kararları →
en iyi / en kötü videolar ve kaynaklar → coğrafya → qwen vs explore.
"""
import json
import statistics
from collections import Counter, defaultdict

from analytics import relative, slot_source_comparison
from llm_client import LLMError, get_llm
from log_setup import get_logger
from models import (AccountMetric, Alert, Comment, CycleReport, Decision, OwnedChannels, Post, Source, videos)
from prompt_safety import data_block, system_prompt
from timeutil import fmt_local, utcnow

log = get_logger("reports")


def _followers_change(session, acc_id, since, until):
    rows = (session.query(AccountMetric).filter(AccountMetric.owned_channel_id == acc_id,
                                                AccountMetric.fetched_at >= since, AccountMetric.fetched_at < until,
                                                AccountMetric.followers != None)  # noqa: E711
            .order_by(AccountMetric.fetched_at).all())
    if not rows:
        return None, None
    return rows[-1].followers, rows[-1].followers - rows[0].followers


def _top_countries(raw: str, n=5):
    try:
        data = json.loads(raw or "[]")
        res = []
        for metric in data:
            for b in ((metric.get("total_value") or {}).get("breakdowns") or []):
                for r in b.get("results") or []:
                    res.append(((r.get("dimension_values") or ["?"])[0], r.get("value") or 0))
        return sorted(res, key=lambda x: -x[1])[:n]
    except (ValueError, AttributeError, TypeError):
        return []


def collect_stats(session, since, until, extra: dict = None) -> dict:
    cache = {}
    posts = session.query(Post).filter(Post.created_at >= since, Post.created_at < until).all()
    published = [p for p in posts if p.status == "PUBLISHED"]
    produced = session.query(videos).filter(videos.created_at >= since, videos.created_at < until).count()

    attention = [{"kind": a.kind, "severity": a.severity, "message": a.message, "at": fmt_local(a.created_at)}
                 for a in session.query(Alert).filter_by(resolved=False).order_by(Alert.created_at.desc()).limit(30)]
    risky = session.query(Comment).filter_by(needs_attention=True, attention_resolved=False).count()
    if risky:
        attention.insert(0, {"kind": "RISKY_COMMENTS", "severity": "WARNING",
                             "message": f"{risky} risky comment(s) waiting for you", "at": ""})

    accounts = []
    for acc in session.query(OwnedChannels).order_by(OwnedChannels.id).all():
        acc_posts = [p for p in published if p.owned_channel_id == acc.id]
        views = [m for m in (relative(session, p, 24, now=until, _median_cache=cache) for p in acc_posts) if m is not None]
        followers, delta = _followers_change(session, acc.id, since, until)
        geo = (session.query(AccountMetric).filter_by(owned_channel_id=acc.id)
               .filter(AccountMetric.audience_geo_json != None)  # noqa: E711
               .order_by(AccountMetric.fetched_at.desc()).first())
        accounts.append({"id": acc.id, "name": acc.ig_username or acc.name, "status": acc.status,
                         "pause_reason": acc.pause_reason, "posts": len(acc_posts), "followers": followers,
                         "followers_delta": delta,
                         "mean_relative_views": round(statistics.mean(views), 2) if views else None,
                         "top_countries": _top_countries(geo.audience_geo_json) if geo else []})

    decs = session.query(Decision).filter(Decision.created_at >= since, Decision.created_at < until).all()
    by_task = defaultdict(Counter)
    for d in decs:
        by_task[d.task]["applied" if d.applied else "rejected"] += 1
    notable = [{"task": d.task, "action": d.proposed_action, "reason": d.reason, "validation": d.validation}
               for d in decs if d.task not in ("clip_score", "caption_write", "cover_select", "comment_classify")][-20:]

    ranked = []
    for p in published:
        r = relative(session, p, 24, now=until, _median_cache=cache)
        if r is not None:
            v = session.get(videos, p.video_id)
            ranked.append({"post_id": p.id, "account_id": p.owned_channel_id, "relative": round(r, 2),
                           "topic": v.topic if v else "", "permalink": p.permalink, "source_id": p.source_id})
    ranked.sort(key=lambda x: -x["relative"])
    src_rel = defaultdict(list)
    for r in ranked:
        if r["source_id"]:
            src_rel[r["source_id"]].append(r["relative"])
    src_rank = sorted(((session.get(Source, sid), statistics.mean(v), len(v)) for sid, v in src_rel.items()),
                      key=lambda x: -x[1])
    src_rows = [{"source": s.handle or s.channel_url, "status": s.status, "mean_relative": round(m, 2), "n": n}
                for s, m, n in src_rank if s]
    extra = extra or {}
    return {
        "window": {"since": fmt_local(since), "until": fmt_local(until)},
        "attention": attention,
        "summary": {"produced": produced, "published": len(published),
                    "failed": sum(1 for p in posts if p.status == "FAILED"),
                    "render_failed": extra.get("render_failed", 0),
                    "dry_run_posts": sum(1 for p in published if p.dry_run),
                    "cycle_duration_h": extra.get("cycle_duration_h")},
        "accounts": accounts,
        "decisions": {"by_task": {k: dict(v) for k, v in by_task.items()}, "notable": notable},
        "best_videos": ranked[:5], "worst_videos": ranked[-5:][::-1] if len(ranked) > 5 else [],
        "best_sources": src_rows[:5], "worst_sources": src_rows[-5:][::-1] if len(src_rows) > 5 else [],
        "slot_comparison": slot_source_comparison(session, since, until),
        "sources_pool": dict(Counter(s.status for s in session.query(Source).all())),
        "cycle": {k: v for k, v in extra.items() if k in ("needs", "produced", "assigned", "scheduled",
                                                          "youtube_blocked", "source_errors")},
    }


_ROLE = """You write a short cycle report (Turkish) for the owner of several Instagram Reels pages.
Use ONLY the numbers in the data. 5-8 sentences: what happened, what needs attention first, notable
decisions and their reasons, one concrete suggestion. No invented numbers."""
_SPEC = '{"narrative": "<Turkish, 5-8 sentences>", "decision_summary": "<Turkish, 1-3 sentences>"}'


def _fallback_narrative(stats: dict) -> dict:
    s = stats["summary"]
    att = len(stats["attention"])
    return {"narrative": f"Bu döngüde {s['produced']} klip üretildi, {s['published']} paylaşım yapıldı, "
                         f"{s['failed']} paylaşım başarısız oldu. Dikkat gerektiren {att} madde var.",
            "decision_summary": ", ".join(f"{k}: {v}" for k, v in stats["decisions"]["by_task"].items()) or "Karar yok."}


def build_cycle_report(session, cycle_id: int, since, until, extra: dict = None, llm=None) -> CycleReport:
    stats = collect_stats(session, since, until, extra)
    llm = llm or get_llm()
    msgs = [{"role": "system", "content": system_prompt(_ROLE, _SPEC)},
            {"role": "user", "content": data_block("CYCLE_STATS", json.dumps(stats, default=str), 10000)}]
    try:
        obj = llm.chat_json(msgs, task="report_narrative", max_tokens=700,
                            validator=lambda o: [] if isinstance(o.get("narrative"), str) and len(o["narrative"]) <= 6000
                            else ["narrative must be a string ≤ 6000 chars"])
    except LLMError as e:
        log.warning("report narrative fallback: %s", e)
        obj = _fallback_narrative(stats)
    rep = CycleReport(cycle_id=cycle_id, started_at=since, finished_at=until,
                      stats_json=json.dumps(stats, default=str), narrative=obj.get("narrative"),
                      decision_summary=obj.get("decision_summary"), created_at=utcnow())
    session.add(rep)
    session.commit()
    log.info("cycle report %s written", rep.id)
    return rep
