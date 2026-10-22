"""Step B: fast lane. (1) real fast_lane() on the copy DB + real latest-3 check of every TRIAL source (metadata only)
(2) on a scratch copy: a TRIAL source promoted to ACTIVE/score 0.9 whose last_video_seen is its 2nd newest upload
→ the real newest upload is "new" → priority clip (stand-in pipeline reuses a rendered reel: download budget spent)
→ scheduler.insert_priority → the clip takes the account's earliest slot."""
import json
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

MODE = sys.argv[1] if len(sys.argv) > 1 else "real"
RUN = common.activate()
if MODE == "priority":
    scratch = os.path.join(RUN, "fastlane_B.db")
    if os.path.exists(scratch):
        os.remove(scratch)
    a, b = sqlite3.connect(os.path.join(RUN, "automation_after_D.db")), sqlite3.connect(scratch)
    a.backup(b)
    b.close()
    a.close()
    os.environ["DATABASE_URL"] = "sqlite:///" + scratch.replace("\\", "/")
common.set_step(f"B-{MODE}")

import models  # noqa: E402
import sources_manager as sm  # noqa: E402
from models import OwnedChannels, Source, UploadQueue, videos  # noqa: E402
from production import ClipCandidate  # noqa: E402
from timeutil import fmt_local, utcnow  # noqa: E402


def real_mode():
    s = models.Session()
    yt = sm.YouTubeAPI()
    out = sm.fast_lane(s, utcnow(), yt=yt)
    latest = []
    for src in s.query(Source).filter(Source.status.in_(("TRIAL", "ACTIVE"))).all():
        meta = yt.channel_videos(src.channel_url, 3)
        latest.append({"source": src.title, "status": src.status, "newest_3": [
            {"id": e.get("id"), "title": e.get("title"), "duration": e.get("duration")}
            for e in (meta.get("entries") or [])][:3]})
        yt.delay()
    common.save_result("B_real", {"fast_lane": out, "active_sources": s.query(Source).filter_by(status="ACTIVE").count(),
                                  "latest_uploads": latest})
    print(json.dumps({"fast_lane": out, "latest": latest}, indent=1, ensure_ascii=False))


def priority_mode():
    s = models.Session()
    yt = sm.YouTubeAPI()
    # a source whose newest upload is NOT used yet (a used one is skipped by design — see B_real/first try)
    used = sm._used_urls(s)
    skipped = []
    for v_any in s.query(videos).filter(videos.qc_status == "PASS", videos.owned_channel_id != None):  # noqa: E711
        src = s.get(Source, v_any.source_id)
        if src.title in [x["source"] for x in skipped]:
            continue
        meta = yt.channel_videos(src.channel_url, 3)
        yt.delay()
        ids = [e["id"] for e in meta.get("entries") or [] if e and e.get("id")]
        if ids and f"https://www.youtube.com/watch?v={ids[0]}" not in used:
            break
        skipped.append({"source": src.title, "newest": ids[:1], "reason": "newest upload already used"})
    acc = s.get(OwnedChannels, v_any.owned_channel_id)
    src.status, src.score, src.last_video_seen = "ACTIVE", 0.9, ids[1] if len(ids) > 1 else None
    s.commit()
    before = [(q.video_id, str(q.scheduled_for), q.priority) for q in
              s.query(UploadQueue).filter_by(owned_channel_id=acc.id).order_by(UploadQueue.scheduled_for)]
    kept = os.path.join(RUN, "for_I", os.path.basename(v_any.file_path))
    reel_copy = os.path.join(RUN, "work", "fastlane_reel.mp4")
    shutil.copy2(kept, reel_copy)

    class StandInPipeline:
        def __init__(self, **hooks):
            self.h = hooks

        def run(self, plan):
            for sv in plan:
                cand = ClipCandidate(sv, {"path": reel_copy, "offset": 0.0}, 30.0, 52.0, 8, topic="fast lane test",
                                     caption={"caption": f"Fast lane test clip\n\n#test #news #clip\n🎥 @{sv.source_handle}",
                                              "comment": "Fast lane test clip", "hashtags": ["#test", "#news", "#clip"]},
                                     out_path=reel_copy, render_info={"duration": 22.0})
                self.h["on_clip_ready"](cand)
    out = sm.fast_lane(s, utcnow(), yt=yt, pipeline_factory=StandInPipeline)
    after = [(q.video_id, fmt_local(q.scheduled_for), q.priority, s.get(videos, q.video_id).time_sensitive) for q in
             s.query(UploadQueue).filter_by(owned_channel_id=acc.id).order_by(UploadQueue.scheduled_for)]
    first = s.query(UploadQueue).filter_by(owned_channel_id=acc.id).order_by(UploadQueue.scheduled_for).first()
    first_v = s.get(videos, first.video_id)
    res = {"source": src.title, "newest_ids": ids, "skipped_sources": skipped, "fast_lane": out, "account": acc.name, "queue_before": before,
           "queue_after": after, "priority_first": bool(first_v.time_sensitive and first.priority == 1),
           "new_video_source_url": first_v.source_url}
    common.save_result("B_priority", res)
    print(json.dumps(res, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    real_mode() if MODE == "real" else priority_mode()
