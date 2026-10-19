"""
scheduler.py — when each account posts what.

Posting window default 15:00–05:00 Europe/Warsaw (account.posting_window to
override); everything stored in UTC. 2-hour slots keyed by local start hour.

Per cycle:
  1. slot table: per account (niche aggregate when the account has little data)
     → n posts + mean relative views@24h per slot (+ follower online hours)
  2. Qwen proposes up to 4 slots per account (brain task "slot_plan"); code only
     accepts slots backed by ≥ 4 posts. No decidable data → no Qwen call.
  3. placement: exactly EXPLORE_SHARE (25 %) of an account's posts go to slots
     Qwen did NOT pick (slot_source="explore"), the rest to Qwen's slots
     ("qwen") or, without a plan, to any slot ("default"). Random minute inside
     the slot, ≥ 8 min from every other scheduled post (all accounts).
Missed posts (PC was off): spread over the rest of today's window within the
day's capacity; otherwise appended to the end of the account's queue.
"""
import datetime as _dt
import json
import random
from collections import defaultdict

import guardrails
import system_state
from analytics import online_followers, slot_performance
from log_setup import get_logger
from models import OwnedChannels, UploadQueue, videos
from prompt_safety import data_block, system_prompt
from timeutil import slots_for, to_local, utcnow, window_for

log = get_logger("scheduler")

MIN_POSTS_PER_SLOT = 4
ACCOUNT_DATA_MIN = 8            # below this the niche aggregate is used
HORIZON_DAYS = 6                # 2 cycles
LEAD = _dt.timedelta(minutes=10)
MISSED_GRACE = _dt.timedelta(minutes=15)
PLAN_MAX_AGE = _dt.timedelta(days=7)
QUEUED_STATES = ("PENDING", "PUBLISHING", "HELD", "UPLOADED")


# ── slot table + Qwen plan ───────────────────────────────────────────────────

def schedulable_accounts(session):
    from production_store import producing_accounts
    return producing_accounts(session)


def build_slot_table(session, accounts, now=None) -> dict:
    now = now or utcnow()
    by_sector = defaultdict(list)
    for a in accounts:
        by_sector[a.sector_id].append(a.id)
    niche_perf = {sid: slot_performance(session, ids, now) for sid, ids in by_sector.items()}
    rows = []
    for a in accounts:
        perf = slot_performance(session, [a.id], now)
        basis = "account"
        if sum(r["n"] for r in perf.values()) < ACCOUNT_DATA_MIN:
            perf, basis = niche_perf.get(a.sector_id, {}), "niche"
        rows.append({"account_id": a.id, "basis": basis,
                     "slots": [{"slot": k, **v} for k, v in sorted(perf.items())],
                     "followers_online_by_local_hour": online_followers(session, a.id)})
    return {"metric": "views at 24h ÷ account 14-day median (1.0 = typical)", "accounts": rows}


_PLAN_ROLE = """You plan Instagram Reels posting times. For each account pick 1-4 two-hour slots
(keyed by local start hour, Europe/Warsaw) with the best relative performance. Only pick slots
whose n >= 4 (fewer posts are not evidence). Prefer consistency; small differences are noise."""
_PLAN_SPEC = """{"actions": [{"type": "slot_assign", "account_id": <int>, "slots": [<slot keys>]}],
 "reason": "<short justification>"}"""


def plan_slots(session, now=None, llm=None) -> dict:
    from brain.core import run_decision
    now = now or utcnow()
    accounts = schedulable_accounts(session)
    table = build_slot_table(session, accounts, now)
    decidable = {}
    for row in table["accounts"]:
        keys = [s["slot"] for s in row["slots"] if s["n"] >= MIN_POSTS_PER_SLOT]
        if keys:
            decidable[row["account_id"]] = keys
    if not decidable:
        log.info("slot plan: not enough data (≥%d posts per slot) — default placement", MIN_POSTS_PER_SLOT)
        return {}
    table["accounts"] = [r for r in table["accounts"] if r["account_id"] in decidable]
    all_keys = sorted({k for ks in decidable.values() for k in ks})
    out = run_decision(session, "slot_plan", system_prompt(_PLAN_ROLE, _PLAN_SPEC),
                       data_block("SLOT_TABLE", json.dumps(table)), allowed={"slot_assign"},
                       ctx={"account_ids": list(decidable), "valid_slots": all_keys,
                            "valid_slots_by_account": decidable},
                       input_summary=f"{len(decidable)} accounts with slot data", llm=llm)
    return {a["account_id"]: a["slots"] for a in out.applied}


def qwen_slots(session, account_id: int, now=None):
    now = now or utcnow()
    raw = system_state.get(f"slot_plan:{account_id}", None, session)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        if now - _dt.datetime.fromisoformat(d["at"]) > PLAN_MAX_AGE:
            return None
        return list(d["slots"])
    except (ValueError, KeyError):
        return None


# ── placement helpers ────────────────────────────────────────────────────────

def taken_times(session) -> list:
    rows = (session.query(UploadQueue.scheduled_for)
            .filter(UploadQueue.status.in_(("PENDING", "PUBLISHING", "UPLOADED"))).all())
    return sorted(r[0] for r in rows if r[0])


def pick_time(start, end, taken, rng, attempts: int = 40):
    span = (end - start).total_seconds()
    if span <= 60:
        return None
    for _ in range(attempts):
        t = start + _dt.timedelta(seconds=rng.uniform(30, span - 30))
        t = t.replace(second=0, microsecond=0)
        if all(abs((t - x).total_seconds()) >= guardrails.MIN_GAP_BETWEEN_POSTS.total_seconds() for x in taken):
            return t
    return None


def explore_due(session, account_id: int) -> bool:
    rows = (session.query(UploadQueue.slot_source).filter(UploadQueue.owned_channel_id == account_id,
                                                          UploadQueue.status.in_(QUEUED_STATES)).all())
    total = len(rows)
    explore = sum(1 for (s,) in rows if s == "explore")
    return explore < round(guardrails.EXPLORE_SHARE * (total + 1))


def _cap_for(session, acc, now):
    if acc.status == "ACTIVE":
        return guardrails.effective_cap(session, acc, now)
    return guardrails.DAILY_CAP_START


def _ready_at(acc):
    return acc.ig_setup_completed_at + guardrails.WARMUP if acc.ig_setup_completed_at else None


def unscheduled_videos(session, acc):
    queued = {r[0] for r in session.query(UploadQueue.video_id).filter(UploadQueue.status.in_(
        QUEUED_STATES + ("SKIPPED",))).all()}
    vids = (session.query(videos).filter(videos.owned_channel_id == acc.id, videos.qc_status == "PASS",
                                         videos.is_used == False)  # noqa: E712
            .order_by(videos.time_sensitive.desc(), videos.qwen_point.desc(), videos.id).all())
    return [v for v in vids if v.id not in queued]


def schedule_account(session, acc, now=None, rng=None, horizon_days: int = HORIZON_DAYS) -> int:
    now = now or utcnow()
    rng = rng or random.Random()
    todo = unscheduled_videos(session, acc)
    if not todo or acc.status not in ("ACTIVE", "WARMUP"):
        return 0
    ready = _ready_at(acc)
    if ready is None:
        return 0
    cap = _cap_for(session, acc, now)
    plan = qwen_slots(session, acc.id, now)
    taken = taken_times(session)
    created = 0
    first_day = to_local(now).date() - _dt.timedelta(days=1)
    for d in range(horizon_days + 1):          # yesterday's window (may still be open) + horizon
        if not todo:
            break
        day = first_day + _dt.timedelta(days=d)
        w_start, w_end = window_for(day, acc.posting_window)
        if w_end <= max(now + LEAD, ready):
            continue
        used = (session.query(UploadQueue).filter(UploadQueue.owned_channel_id == acc.id,
                                                  UploadQueue.status.in_(("PENDING", "PUBLISHING", "UPLOADED")),
                                                  UploadQueue.scheduled_for >= w_start,
                                                  UploadQueue.scheduled_for < w_end).count())
        avail = [(k, max(s, now + LEAD, ready), e) for k, s, e in slots_for(day, acc.posting_window)
                 if e > max(now + LEAD, ready) + _dt.timedelta(minutes=5)]
        for _ in range(max(0, cap - used)):
            if not todo or not avail:
                break
            explore = explore_due(session, acc.id)
            plan_keys = [k for k, _, _ in avail if plan and k in plan]
            if explore:
                pool = [a for a in avail if not plan or a[0] not in plan] or avail
                source = "explore"
            elif plan_keys:
                pool = [a for a in avail if a[0] in plan_keys]
                source = "qwen"
            else:
                pool = avail
                source = "default"
            rng.shuffle(pool)
            t = None
            for k, s, e in pool:
                t = pick_time(s, e, taken, rng)
                if t:
                    break
            if not t:
                break
            v = todo.pop(0)
            session.add(UploadQueue(owned_channel_id=acc.id, video_id=v.id, scheduled_for=t, status="PENDING",
                                    created_at=now, slot_source=source, priority=1 if v.time_sensitive else 0,
                                    cycle_id=v.cycle_id))
            session.flush()
            taken.append(t)
            created += 1
    session.commit()
    return created


def reschedule_missed(session, now=None, rng=None) -> dict:
    """Missed items are never bulk-published: spread over today's remaining window, else re-queued at the end."""
    now = now or utcnow()
    rng = rng or random.Random()
    missed = (session.query(UploadQueue).filter(UploadQueue.status == "PENDING",
                                                UploadQueue.scheduled_for < now - MISSED_GRACE)
              .order_by(UploadQueue.scheduled_for).all())
    out = {"respread": 0, "requeued": 0}
    by_acc = defaultdict(list)
    for m in missed:
        by_acc[m.owned_channel_id].append(m)
    for acc_id, items in by_acc.items():
        acc = session.get(OwnedChannels, acc_id)
        from timeutil import window_day_of
        day = window_day_of(now, acc.posting_window)
        room = 0
        w_end = None
        if day is not None:
            _, w_end = window_for(day, acc.posting_window)
            pending_today = (session.query(UploadQueue).filter(
                UploadQueue.owned_channel_id == acc_id, UploadQueue.status == "PENDING",
                UploadQueue.scheduled_for >= now - MISSED_GRACE, UploadQueue.scheduled_for < w_end).count())
            room = max(0, _cap_for(session, acc, now) - guardrails.posts_last_24h(session, acc_id, now) - pending_today)
        taken = [t for t in taken_times(session) if t >= now]
        spread = items[:room]
        if spread and w_end:
            step = (w_end - LEAD - now) / (len(spread) + 1)
            for i, it in enumerate(spread):
                s = now + LEAD / 2 + step * i
                t = pick_time(s, s + step, taken, rng) or (s + step / 2)
                it.scheduled_for = t
                taken.append(t)
                out["respread"] += 1
        for it in items[len(spread):]:
            session.delete(it)                    # video becomes unscheduled → end of the queue
            out["requeued"] += 1
        session.flush()
        if len(items) > len(spread):
            schedule_account(session, acc, now, rng)
    session.commit()
    if missed:
        log.info("missed posts: %s", out)
    return out


def insert_priority(session, acc, now=None, rng=None) -> int:
    """Fast lane: rebuild the account's PENDING queue so time-sensitive clips take the earliest slots."""
    now = now or utcnow()
    session.query(UploadQueue).filter(UploadQueue.owned_channel_id == acc.id, UploadQueue.status == "PENDING",
                                      UploadQueue.scheduled_for > now).delete()
    session.commit()
    return schedule_account(session, acc, now, rng)


def schedule_all(session, now=None, llm=None, rng=None) -> int:
    now = now or utcnow()
    try:
        plan_slots(session, now, llm)
    except Exception as e:
        log.warning("slot planning failed, keeping previous plan: %s", e)
    reschedule_missed(session, now, rng)
    total = 0
    for acc in schedulable_accounts(session):
        guardrails.refresh_account_status(acc, now)
        total += schedule_account(session, acc, now, rng)
    log.info("scheduled %d new posts", total)
    return total


def plan_for_display(session, local_day=None):
    """Rows for the panel's 'today' page."""
    from timeutil import fmt_local
    day = local_day or to_local(utcnow()).date()
    s, _ = window_for(day)
    _, e = window_for(day + _dt.timedelta(days=1))
    rows = (session.query(UploadQueue).filter(UploadQueue.scheduled_for >= s, UploadQueue.scheduled_for < e)
            .order_by(UploadQueue.scheduled_for).all())
    return [{"time": fmt_local(r.scheduled_for, "%a %H:%M"), "account_id": r.owned_channel_id,
             "video_id": r.video_id, "status": r.status, "slot_source": r.slot_source, "priority": r.priority}
            for r in rows]
