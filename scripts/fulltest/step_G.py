"""Step G: synthetic-but-varied insights → relative performance, early warning → Qwen account review,
trial evaluation, bandit, next cycle's Qwen slot plan. All Qwen decisions + validations are collected."""
import datetime as dt
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

RUN = common.activate()
common.set_step("G")

import analytics  # noqa: E402
import models  # noqa: E402
import scheduler  # noqa: E402
import sources_manager as sm  # noqa: E402
from brain.actions import FORBIDDEN_ACTIONS  # noqa: E402
from models import Decision, OwnedChannels, Post, PostMetric, Source, videos  # noqa: E402
from timeutil import local_to_utc, to_local  # noqa: E402


def add_metrics(s, p, views, rng):
    for h, f in ((1, 0.15), (24, 0.8), (72, 1.0)):
        v = int(views * f * rng.uniform(0.9, 1.1))
        s.add(PostMetric(post_id=p.id, checkpoint_h=h, views=v, reach=int(v * 0.8), likes=int(v * 0.05),
                         comments=int(v * 0.004), shares=int(v * 0.01), saved=int(v * 0.008),
                         avg_watch_time_ms=rng.randint(6000, 14000)))


def history(s, acc, start, n, reach, slots, rng):
    """n older published posts (dummy video rows) in the given Warsaw slots."""
    for i in range(n):
        day = to_local(start).date() - dt.timedelta(days=4 + i)
        slot = slots[i % len(slots)]
        t = local_to_utc(dt.datetime.combine(day, dt.time(slot % 24, 30)))
        v = videos(sector_id=acc.sector_id, owned_channel_id=acc.id, qwen_point=8, qc_status="HIST", is_used=True,
                   caption=f"history {acc.id}-{i}")
        s.add(v)
        s.flush()
        p = Post(owned_channel_id=acc.id, video_id=v.id, status="PUBLISHED", published_at=t, created_at=t,
                 slot_hour=slot, slot_source="default", caption=v.caption, dry_run=True)
        s.add(p)
        s.flush()
        add_metrics(s, p, reach * rng.uniform(0.8 if slot == slots[0] else 0.5, 1.2 if slot == slots[0] else 0.7), rng)


def main():
    client = common.instrumented_llm()
    s = models.Session()
    rng = random.Random(7)
    t0 = time.time()
    posts = s.query(Post).filter_by(status="PUBLISHED").order_by(Post.published_at).all()
    sources = sorted({p.source_id for p in posts if p.source_id})
    bad_source = sources[-1] if sources else None
    accs = sorted({p.owned_channel_id for p in posts})
    drop_acc, steady_acc = accs[0], (accs[1] if len(accs) > 1 else accs[0])
    first, last = posts[0].published_at, posts[-1].published_at
    # history (before this cycle): drop_acc had ~10k reach, steady_acc ~3k; 2 slots each so a slot plan is possible
    history(s, s.get(OwnedChannels, drop_acc), first, 8, 10000, [19, 23], rng)
    history(s, s.get(OwnedChannels, steady_acc), first, 8, 3000, [17, 21], rng)
    # this cycle: drop_acc collapses to ~700; bad_source clips ~400; others normal
    for p in posts:
        base = {drop_acc: 700}.get(p.owned_channel_id, 3000)
        if p.source_id == bad_source:
            base = 400
        add_metrics(s, p, base, rng)
    s.commit()

    now_warning = last + dt.timedelta(hours=24)           # recent posts inside the 3-day window
    now_eval = last + dt.timedelta(hours=72, minutes=5)   # 72 h after the last trial post
    rel = {p.id: analytics.relative(s, p, 24, now=now_warning) for p in posts}
    warnings = {a: analytics.early_warning_active(s, s.get(OwnedChannels, a), now_warning) for a in accs}
    review = analytics.review_accounts(s, now_warning, llm=client)
    trials = sm.evaluate_trials(s, now_eval)
    sm.update_bandit(s, now_eval)
    bandit = [{"source": x.title, "status": x.status, "alpha": x.bandit_alpha, "beta": x.bandit_beta,
               "mean": round(x.bandit_alpha / (x.bandit_alpha + x.bandit_beta), 2)} for x in s.query(Source).all()
              if x.status in ("ACTIVE", "TRIAL", "REJECTED")]
    picks = {}
    for sid in {x.sector_id for x in s.query(Source).filter(Source.status.in_(("ACTIVE", "TRIAL"))).all()}:
        r = random.Random(1)
        picks[sid] = [x.title for x in sm.pick_sources(s, sid, 20, r)]
    slot_table = scheduler.build_slot_table(s, scheduler.schedulable_accounts(s), now_eval)
    plan = scheduler.plan_slots(s, now_eval, llm=client)
    decisions = [{"id": d.id, "task": d.task, "account": d.owned_channel_id, "proposed": d.proposed_action,
                  "reason": d.reason, "validation": d.validation, "applied": d.applied}
                 for d in s.query(Decision).filter(Decision.task.in_(["account_review", "slot_plan", "source_trial_result",
                                                                        "slot_assign"])).order_by(Decision.id)]
    proposed_types = []
    for d in decisions:
        try:
            proposed_types.append(json.loads(d["proposed"] or "{}").get("type"))
        except ValueError:
            pass
    result = {
        "seconds": round(time.time() - t0, 1), "drop_account": drop_acc, "steady_account": steady_acc,
        "bad_source": bad_source, "relative_24h": rel, "early_warning": warnings, "account_review": review,
        "accounts_after": [(a.id, a.name, a.status, a.pause_reason, a.daily_post_cap)
                           for a in s.query(OwnedChannels).filter(OwnedChannels.name.like("test_%"))],
        "trials": trials, "sources_after": [(x.title, x.status, x.status_reason) for x in s.query(Source).all()],
        "bandit": bandit, "bandit_picks_20": picks, "slot_table": slot_table, "slot_plan": plan,
        "decisions": decisions,
        "forbidden_proposed": [t for t in proposed_types if t in FORBIDDEN_ACTIONS],
        "rejected": [d for d in decisions if d["validation"] and d["validation"].startswith("REJECTED")],
        "qwen": common.qwen_stats("G"), "resources": common.sample_resources("G end")}
    common.save_result("G", result)
    print(json.dumps({k: result[k] for k in ("early_warning", "account_review", "accounts_after", "trials",
                                             "slot_plan", "forbidden_proposed", "rejected", "qwen")},
                     indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
