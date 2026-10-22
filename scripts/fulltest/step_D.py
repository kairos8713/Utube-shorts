"""Step D: validate the schedule produced by C's cycle (scheduler.schedule_all) and snapshot the DB for step I."""
import datetime as dt
import json
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

RUN = common.activate()
common.set_step("D")

import guardrails  # noqa: E402
import models  # noqa: E402
import scheduler  # noqa: E402
import system_state  # noqa: E402
from models import OwnedChannels, UploadQueue, videos  # noqa: E402
from timeutil import fmt_local, slot_key_of, utcnow, window_day_of  # noqa: E402


def main():
    s = models.Session()
    rows = s.query(UploadQueue).order_by(UploadQueue.scheduled_for).all()
    accs = {a.id: a for a in s.query(OwnedChannels).all()}
    checks, problems = {}, []
    times = [r.scheduled_for for r in rows]
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])]
    checks["count"] = len(rows)
    checks["all_in_window"] = all(window_day_of(t) is not None for t in times)
    checks["min_gap_min"] = round(min(gaps), 1) if gaps else None
    checks["gap_ok"] = all(g >= guardrails.MIN_GAP_BETWEEN_POSTS.total_seconds() / 60 for g in gaps)
    per_acc_day = defaultdict(list)
    for r in rows:
        per_acc_day[(r.owned_channel_id, window_day_of(r.scheduled_for))].append(r.id)
    checks["cap_1_per_window_day"] = all(len(v) <= 1 for v in per_acc_day.values())
    checks["warmup_ok"] = all(r.scheduled_for >= accs[r.owned_channel_id].ig_setup_completed_at + guardrails.WARMUP
                              for r in rows)
    vids = [r.video_id for r in rows]
    checks["each_video_once"] = len(vids) == len(set(vids))
    owners_ok = all(s.get(videos, r.video_id).owned_channel_id == r.owned_channel_id for r in rows)
    checks["video_owner_matches_account"] = owners_ok
    by_src = defaultdict(int)
    for r in rows:
        by_src[r.slot_source] += 1
    checks["slot_sources"] = dict(by_src)
    checks["explore_share"] = round(by_src.get("explore", 0) / max(len(rows), 1), 2)
    plans = {a: system_state.get(f"slot_plan:{a}", None, s) for a in accs}
    table = [{"account": accs[r.owned_channel_id].name, "warsaw": fmt_local(r.scheduled_for, "%a %d %b %H:%M"),
              "utc": str(r.scheduled_for), "slot": slot_key_of(r.scheduled_for), "slot_source": r.slot_source,
              "video_id": r.video_id, "status": r.status, "priority": r.priority} for r in rows]
    for k, v in checks.items():
        if v is False:
            problems.append(k)
    common.save_result("D", {"checks": checks, "problems": problems, "table": table, "qwen_slot_plans": plans,
                             "now_utc": str(utcnow()), "resources": common.sample_resources("D end")})
    # snapshot for step I (services in real time on an unpublished queue)
    src = sqlite3.connect(os.path.join(RUN, "automation_copy.db"))
    dst = sqlite3.connect(os.path.join(RUN, "automation_after_D.db"))
    src.backup(dst)
    dst.close()
    src.close()
    print(json.dumps(checks, indent=1))
    for t in table:
        print(f"  {t['account']:24} {t['warsaw']:18} slot {t['slot']:>2} {t['slot_source']:8} video {t['video_id']}")
    print("problems:", problems)


if __name__ == "__main__":
    main()
