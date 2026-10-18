"""
production_store.py — DB side of the production pipeline.

ProductionStore supplies the Pipeline hooks (dedup, recent captions, cover +
phash, persistence). assign_videos() gives every new clip to exactly ONE
account of its niche (largest inventory deficit first; clips of a TRIAL source
are spread over different accounts).
"""
import json
import os
from collections import defaultdict

import cover_frame
import dedup
from brain.core import log_decision
from config import settings
from log_setup import get_logger
from models import OwnedChannels, Sectors, Source, UploadQueue, videos
from timeutil import utcnow

log = get_logger("production_store")


class ProductionStore:
    def __init__(self, session, cycle_id: int):
        self.s = session
        self.cycle_id = cycle_id

    # ── Pipeline hooks ───────────────────────────────────────────────────────
    def is_duplicate(self, url, start, end) -> bool:
        return dedup.clip_conflict(self.s, url, start, end) is not None

    def previous_captions(self, niche) -> list:
        rows = (self.s.query(videos.caption).filter(videos.caption != None)  # noqa: E711
                .order_by(videos.id.desc()).limit(50).all())
        return [r[0] for r in rows]

    def post_render(self, cand) -> dict:
        extras = {"phash": dedup.clip_phash(cand.segment["path"], cand.start, cand.end)}
        cover = cover_frame.select_cover(cand.out_path, cand.render_info.get("duration", cand.end - cand.start),
                                         os.path.join(settings.REELS_DIR, "covers"))
        if cover:
            extras["cover_offset_ms"] = cover["offset_ms"]
            extras["cover_path"] = cover["path"]
            extras["cover_score"] = cover["score"]
        return extras

    def on_clip_ready(self, cand):
        src = cand.source
        phash = cand.extras.get("phash")
        conflict = dedup.clip_conflict(self.s, src.url, cand.abs_start, cand.abs_end, phash)
        if conflict:
            log.info("dropping duplicate clip: %s", conflict)
            for p in (cand.out_path, cand.extras.get("cover_path")):
                if p and os.path.exists(p):
                    os.remove(p)
            return None
        sector = self.s.query(Sectors).filter_by(name=src.niche).first()
        cap = cand.caption or {}
        v = videos(
            sector_id=sector.id if sector else None, qwen_point=cand.score, real_point=0,
            source_id=src.source_id, source_url=src.url,
            segment_start=round(cand.abs_start, 3), segment_end=round(cand.abs_end, 3),
            phash=phash, file_path=cand.out_path, cover_path=cand.extras.get("cover_path"),
            cover_offset_ms=cand.extras.get("cover_offset_ms"), transcript=cand.transcript[:8000],
            topic=cand.topic, caption=cap.get("caption"), hashtags=" ".join(cap.get("hashtags", [])),
            summary_text=cap.get("comment"), qc_status="PASS",
            qc_report=json.dumps({"metrics": cand.qc.metrics if cand.qc else {},
                                  "warnings": cand.qc.warnings if cand.qc else [],
                                  "rife_used": cand.render_info.get("rife_used")}, default=str)[:8000],
            cycle_id=self.cycle_id, time_sensitive=cand.time_sensitive,
            clip_duration=cand.render_info.get("duration"), is_used=False, created_at=utcnow(),
            render_time_seconds=cand.render_info.get("elapsed"),
        )
        self.s.add(v)
        self.s.flush()
        log_decision(self.s, "clip_score", {"video_id": v.id, "score": cand.score, "topic": cand.topic,
                                            "segment": [v.segment_start, v.segment_end]},
                     "OK", True, cand.reason, input_summary=src.url)
        log_decision(self.s, "caption_write", {"video_id": v.id, "caption": v.caption}, "OK", True)
        if cand.extras.get("cover_offset_ms") is not None:
            log_decision(self.s, "cover_select", {"video_id": v.id, "offset_ms": v.cover_offset_ms,
                                                  "score": cand.extras.get("cover_score")}, "OK", True)
        self.s.commit()
        return v

    def on_clip_failed(self, cand, reason: str):
        log.warning("clip failed (%s @%.0fs): %s", cand.source.url, cand.abs_start, reason)


# ── inventory / assignment ───────────────────────────────────────────────────

def account_inventory(session, account_id: int) -> int:
    """Clips assigned to the account that are not yet published/failed."""
    used = {r[0] for r in session.query(UploadQueue.video_id)
            .filter(UploadQueue.owned_channel_id == account_id,
                    UploadQueue.status.in_(("UPLOADED", "FAILED", "SKIPPED"))).all()}
    q = session.query(videos.id).filter(videos.owned_channel_id == account_id, videos.qc_status == "PASS",
                                        videos.is_used == False)  # noqa: E712
    return len([vid for (vid,) in q.all() if vid not in used])


def producing_accounts(session) -> list:
    """Accounts we produce for: Emre entered the setup date and they are not paused/disconnected."""
    return (session.query(OwnedChannels)
            .filter(OwnedChannels.ig_setup_completed_at != None,  # noqa: E711
                    OwnedChannels.status.in_(("ACTIVE", "WARMUP"))).order_by(OwnedChannels.id).all())


def assign_videos(session) -> int:
    pending = (session.query(videos).filter(videos.owned_channel_id == None, videos.qc_status == "PASS")  # noqa: E711
               .order_by(videos.time_sensitive.desc(), videos.qwen_point.desc(), videos.id).all())
    if not pending:
        return 0
    accounts = producing_accounts(session)
    by_sector = defaultdict(list)
    for a in accounts:
        by_sector[a.sector_id].append(a)
    inv = {a.id: account_inventory(session, a.id) for a in accounts}
    source_accounts = defaultdict(set)     # source_id → accounts that already got a clip
    for v_src, v_acc in session.query(videos.source_id, videos.owned_channel_id).filter(
            videos.owned_channel_id != None).all():  # noqa: E711
        source_accounts[v_src].add(v_acc)
    trial_ids = {s.id for s in session.query(Source).filter_by(status="TRIAL").all()}
    assigned = 0
    for v in pending:
        cands = by_sector.get(v.sector_id) or []
        if not cands:
            continue
        if v.source_id in trial_ids:
            fresh = [a for a in cands if a.id not in source_accounts[v.source_id]]
            cands = fresh or cands
        acc = min(cands, key=lambda a: (inv[a.id], a.id))
        v.owned_channel_id = acc.id
        inv[acc.id] += 1
        source_accounts[v.source_id].add(acc.id)
        assigned += 1
    session.commit()
    log.info("assigned %d clips to accounts", assigned)
    return assigned
