"""Step E: publishing simulation (DRY_RUN, simulated clock) + error injection with mocked clients.

  python step_E.py plan     # whole plan on the copy DB with the real DRY_RUN client
  python step_E.py inject   # 6 failure scenarios on a scratch DB (inject_E.db) with mocked clients
"""
import datetime as dt
import glob
import json
import os
import shutil
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

MODE = sys.argv[1] if len(sys.argv) > 1 else "plan"
RUN = common.activate()
if MODE == "inject":
    scratch = os.path.join(RUN, "inject_E.db")
    if os.path.exists(scratch):
        os.remove(scratch)
    src = sqlite3.connect(os.path.join(RUN, "automation_after_D.db"))
    dst = sqlite3.connect(scratch)
    src.backup(dst)
    dst.close()
    src.close()
    os.environ["DATABASE_URL"] = "sqlite:///" + scratch.replace("\\", "/")
common.set_step(f"E-{MODE}")

import guardrails  # noqa: E402
import models  # noqa: E402
from instagram import hosting, publisher  # noqa: E402
from instagram.client import client_for  # noqa: E402
from models import Alert, OwnedChannels, Post, Source, UploadQueue, videos  # noqa: E402
from publish_runner import publish_due  # noqa: E402


def plan_mode():
    s = models.Session()
    # keep material for step I (cleanup after publish deletes reels and source downloads)
    keep = os.path.join(RUN, "for_I")
    os.makedirs(keep, exist_ok=True)
    for v in s.query(videos).filter(videos.file_path != None).all():  # noqa: E711
        if os.path.exists(v.file_path):
            shutil.copy2(v.file_path, os.path.join(keep, os.path.basename(v.file_path)))
    dl = sorted(glob.glob(os.path.join(RUN, "downloads", "*.mp4")), key=os.path.getsize)
    if dl:
        shutil.copy2(dl[0], os.path.join(keep, "workload_source.mp4"))

    items = s.query(UploadQueue).order_by(UploadQueue.scheduled_for).all()
    start = min(i.scheduled_for for i in items) - dt.timedelta(minutes=1)
    end = max(i.scheduled_for for i in items) + dt.timedelta(hours=3)
    t, log, last_reconcile = start, [], start
    while t <= end:
        for item_id, outcome in publish_due(s, t, client_factory=client_for, host=hosting, sleep=lambda x: None):
            log.append({"t": str(t), "item": item_id, "outcome": outcome})
        if t - last_reconcile >= dt.timedelta(minutes=10):
            r = publisher.reconcile_uncertain(s, t, client_factory=client_for)
            if any(r.values()):
                log.append({"t": str(t), "reconcile": r})
            last_reconcile = t
        t += dt.timedelta(minutes=2)

    posts = s.query(Post).filter_by(status="PUBLISHED").order_by(Post.published_at).all()
    pub_times = [p.published_at for p in posts]
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(pub_times, pub_times[1:])]
    per_acc = defaultdict(list)
    for p in posts:
        per_acc[p.owned_channel_id].append(p.published_at)
    within_24h = [(a, (y - x).total_seconds() / 3600) for a, ts in per_acc.items() for x, y in zip(ts, ts[1:])
                  if (y - x) < dt.timedelta(hours=24)]
    vids = [p.video_id for p in posts]
    seg_clash = []
    for i, p in enumerate(posts):
        for q in posts[i + 1:]:
            if p.source_url == q.source_url and p.owned_channel_id != q.owned_channel_id and \
                    p.segment_start < q.segment_end and q.segment_start < p.segment_end:
                seg_clash.append((p.id, q.id))
    files_left = [v.id for v in s.query(videos).filter(videos.is_used == True).all()  # noqa: E712
                  if v.file_path and os.path.exists(v.file_path)]
    dry_lines = 0
    for f in glob.glob(os.path.join(RUN, "logs", "ig_client.log*")):
        dry_lines += sum("[DRY_RUN]" in l for l in open(f, encoding="utf-8", errors="replace"))
    violations = os.path.exists(os.path.join(RUN, "network_violations.jsonl"))
    items_after = {i.id: i.status for i in s.query(UploadQueue).all()}
    checks = {"published": len(posts), "queue_items": len(items), "min_gap_min": round(min(gaps), 1) if gaps else None,
              "gap_ok": all(g >= 8 for g in gaps), "accounts_with_2_in_24h": within_24h,
              "video_published_twice": len(vids) != len(set(vids)), "segment_on_two_accounts": seg_clash,
              "reel_files_left_after_publish": files_left, "all_dry_run": all(p.dry_run for p in posts),
              "dry_run_log_lines": dry_lines, "network_violations_file": violations,
              "items_not_uploaded": {k: v for k, v in items_after.items() if v != "UPLOADED"}}
    common.save_result("E_plan", {"checks": checks, "log": log,
                                  "posts": [{"id": p.id, "account": p.owned_channel_id, "video": p.video_id,
                                             "published_at": str(p.published_at), "media": p.ig_media_id,
                                             "slot_source": p.slot_source} for p in posts],
                                  "resources": common.sample_resources("E plan end")})
    print(json.dumps(checks, indent=1, default=str))


def inject_mode():
    sys.path.insert(0, os.path.join(common.ROOT, "tests"))
    from fakes import FakeHost, FakeIG, ig_error
    from instagram import errors as E
    s = models.Session()
    for acc in s.query(OwnedChannels).filter(OwnedChannels.status == "ACTIVE").all():
        acc.consecutive_failures = 0
    s.query(UploadQueue).delete()
    s.commit()
    accs = s.query(OwnedChannels).filter(OwnedChannels.status == "ACTIVE").order_by(OwnedChannels.id).all()
    vids = s.query(videos).filter(videos.qc_status == "PASS").order_by(videos.id).all()
    t0 = max(v.created_at for v in vids) + dt.timedelta(hours=1)
    results = []

    def item_for(n, k):
        v = vids[n % len(vids)]
        dummy = os.path.join(RUN, "work", f"inject_dummy_{k}.mp4")   # one per item: a publish deletes its file
        with open(dummy, "wb") as f:
            f.write(b"0" * 2048)
        v.file_path, v.is_used = dummy, False
        q = UploadQueue(owned_channel_id=v.owned_channel_id, video_id=v.id, status="PENDING",
                        scheduled_for=t0 + dt.timedelta(hours=26 * k))
        s.add(q)
        s.commit()
        return q, v, t0 + dt.timedelta(hours=26 * k)

    def run(name, q, now, ig, host=None, extra=None):
        out = publish_due(s, now, client_factory=lambda acc, **kw: ig, host=host or FakeHost(), sleep=lambda x: None,
                          poll_timeout=0 if name == "6 slow processing" else None)
        return [o for _, o in out]

    # (1) crash before container: post PENDING + item PUBLISHING left behind
    q, v, now = item_for(0, 0)
    p = Post(owned_channel_id=q.owned_channel_id, video_id=v.id, queue_id=q.id, status="PENDING", caption=v.caption,
             created_at=now)
    s.add(p)
    s.flush()
    q.post_id, q.status = p.id, "PUBLISHING"
    s.commit()
    ig = FakeIG()
    r1 = publisher.reconcile_uncertain(s, now + dt.timedelta(minutes=15), lambda acc, **k: ig)
    out1 = run("1", q, now + dt.timedelta(minutes=16), ig)
    results.append({"scenario": "1 crash before container", "reconcile": r1, "outcomes": out1,
                    "item": q.status, "publish_calls": ig.calls.count("publish")})

    # (2) R2 not configured, then fixed
    q, v, now = item_for(1, 1)
    ig = FakeIG()

    class NoR2(FakeHost):
        def upload_temp(self, path, dry_run=None):
            raise hosting.HostingNotConfigured("R2_ENDPOINT_URL missing")
    out2a = run("2", q, now, ig, NoR2())
    state2 = (q.status, q.retry_count)
    out2b = run("2", q, now + dt.timedelta(minutes=31), ig)
    results.append({"scenario": "2 R2 not configured", "outcomes": out2a + out2b, "item_after_r2": state2,
                    "item": q.status, "r2_alerts": s.query(Alert).filter_by(kind="R2_NOT_CONFIGURED").count(),
                    "publish_calls": ig.calls.count("publish")})

    # (3) publish outcome uncertain, media appears later
    q, v, now = item_for(2, 2)
    ig = FakeIG(publish_errors=[ig_error(E.TRANSIENT, "network: read timed out")], recent=[])
    out3 = run("3", q, now, ig)
    held = q.status
    ig.recent = [{"id": "M_LATE", "caption": v.caption}]
    ig.status_seq = ["PUBLISHED"]
    r3 = publisher.reconcile_uncertain(s, now + dt.timedelta(minutes=20), lambda acc, **k: ig)
    results.append({"scenario": "3 publish uncertain", "outcomes": out3, "item_while_uncertain": held,
                    "reconcile": r3, "item": q.status, "publish_calls": ig.calls.count("publish")})

    # (4) auth error → auto pause
    q, v, now = item_for(3, 3)
    acc_id = q.owned_channel_id
    ig = FakeIG(container_error=ig_error(E.AUTH, "Error validating access token"))
    out4 = run("4", q, now, ig)
    acc = s.get(OwnedChannels, acc_id)
    results.append({"scenario": "4 auth error", "outcomes": out4, "account_status": acc.status,
                    "pause_reason": acc.pause_reason, "item": q.status, "publish_calls": ig.calls.count("publish")})
    acc.status, acc.pause_reason, acc.consecutive_failures = "ACTIVE", None, 0     # test-only reset
    s.commit()

    # (5) copyright match
    q, v, now = item_for(4, 4)
    ig = FakeIG(copyright_match=True)
    out5 = run("5", q, now, ig)
    src = s.get(Source, v.source_id) if v.source_id else None
    results.append({"scenario": "5 copyright", "outcomes": out5, "item": q.status,
                    "source_status": src.status if src else None, "publish_calls": ig.calls.count("publish")})

    # (6) slow processing, later OK
    q, v, now = item_for(5, 5)
    acc_before = s.get(OwnedChannels, q.owned_channel_id).consecutive_failures
    ig = FakeIG(status_seq=["IN_PROGRESS"])
    out6a = run("6 slow processing", q, now, ig)
    ig.status_seq = ["FINISHED"]
    out6b = run("6b", q, now + dt.timedelta(minutes=31), ig)
    results.append({"scenario": "6 slow processing", "outcomes": out6a + out6b, "item": q.status,
                    "retry_count": q.retry_count, "account_failures_before_after":
                    (acc_before, s.get(OwnedChannels, q.owned_channel_id).consecutive_failures),
                    "publish_calls": ig.calls.count("publish")})

    stuck = [i.id for i in s.query(UploadQueue).filter_by(status="PUBLISHING").all()]
    pub_per_video = defaultdict(int)
    for p in s.query(Post).filter_by(status="PUBLISHED").all():
        pub_per_video[p.video_id] += 1
    checks = {"stuck_publishing": stuck, "videos_published_twice": [k for k, n in pub_per_video.items() if n > 1],
              "max_publish_calls_per_scenario": max(r["publish_calls"] for r in results)}
    common.save_result("E_inject", {"results": results, "checks": checks})
    print(json.dumps(results, indent=1, default=str))
    print(json.dumps(checks, indent=1))


if __name__ == "__main__":
    plan_mode() if MODE == "plan" else inject_mode()
