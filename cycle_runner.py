"""
cycle_runner.py — THE orchestrator (replaces weekly_cycle.py and
sector_processor.main()). One setting: CYCLE_DAYS.

Every CYCLE_DAYS (anchored in UTC, stored in system_state):
  0. heartbeat, yt-dlp self-update (once), cookies age check, DB migrations
  1. account status refresh (WARMUP → ACTIVE by the warm-up gate)
  2. source discovery / trial evaluation (Phase 7, sources_manager)
  3. production for the NEXT cycle: each producing account is filled up to
     2 × cap × CYCLE_DAYS clips (one-cycle buffer: on day 1 the clips for days
     4–6 are produced while days 1–3 are being published)
  4. assign clips to accounts, schedule them (scheduler)
  5. cycle report (reports)
Then sleep until the next cycle boundary, writing heartbeats.
"""
import datetime as _dt
import json
import time
import traceback

from config import settings
from log_setup import get_logger
from timeutil import utcnow

log = get_logger("cycle")

CYCLE_DAYS = 3
BUFFER_CYCLES = 2            # inventory target = BUFFER_CYCLES × cap × CYCLE_DAYS
ANCHOR_KEY = "cycle_anchor"


def cycle_length() -> _dt.timedelta:
    return _dt.timedelta(days=CYCLE_DAYS)


def get_anchor(session, now=None) -> _dt.datetime:
    import system_state
    v = system_state.get(ANCHOR_KEY, None, session)
    if v:
        return _dt.datetime.fromisoformat(v)
    anchor = (now or utcnow()).replace(minute=0, second=0, microsecond=0)
    system_state.put(ANCHOR_KEY, anchor.isoformat(), session)
    return anchor


def cycle_bounds(anchor: _dt.datetime, now: _dt.datetime):
    """(cycle_index, start, end) of the cycle containing `now` (UTC)."""
    idx = int((now - anchor) // cycle_length()) if now >= anchor else 0
    start = anchor + idx * cycle_length()
    return idx, start, start + cycle_length()


def cycle_id_for(start: _dt.datetime) -> int:
    return int(start.replace(tzinfo=_dt.timezone.utc).timestamp())


def compute_needs(session, now=None) -> dict:
    """{sector_id: clips_needed} so every producing account holds a 2-cycle buffer."""
    import guardrails
    from production_store import account_inventory, producing_accounts
    needs = {}
    for acc in producing_accounts(session):
        cap = max(1, guardrails.effective_cap(session, acc, now) if acc.status == "ACTIVE"
                  else guardrails.DAILY_CAP_START)
        target = BUFFER_CYCLES * cap * CYCLE_DAYS
        gap = max(0, target - account_inventory(session, acc.id))
        if gap:
            needs[acc.sector_id] = needs.get(acc.sector_id, 0) + gap
    return needs


def run_one_cycle(session, now=None, pipeline_factory=None, llm=None) -> dict:
    import downloader
    import guardrails
    import system_state
    from alerts import raise_alert
    from models import OwnedChannels
    from production import Pipeline
    from production_store import ProductionStore, assign_videos

    now = now or utcnow()
    anchor = get_anchor(session, now)
    idx, start, end = cycle_bounds(anchor, now)
    cid = cycle_id_for(start)
    stats = {"cycle_id": cid, "cycle_index": idx, "start": start.isoformat(), "end": end.isoformat(),
             "started_at": now.isoformat()}
    system_state.heartbeat("cycle_runner", session)
    log.info("cycle #%d (%s → %s UTC) starting", idx, start, end)

    # 0. YouTube health
    downloader.update_ytdlp_once()
    age = downloader.cookies_age_days()
    if age > settings.COOKIES_MAX_AGE_DAYS:
        raise_alert("COOKIES_STALE", f"cookies.txt is {age:.0f} days old — refresh it to avoid bot blocks",
                    severity="WARNING", session=session)

    # 1. account states
    for acc in session.query(OwnedChannels).all():
        guardrails.refresh_account_status(acc, now)
    session.commit()

    # 2. sources
    try:
        import sources_manager
        stats["sources"] = sources_manager.cycle_maintenance(session, now, llm=llm)
    except Exception as e:
        log.error("source maintenance failed: %s\n%s", e, traceback.format_exc())
        stats["sources_error"] = str(e)

    # 3. production
    needs = compute_needs(session, now)
    stats["needs"] = needs
    produced = 0
    if needs:
        import sources_manager
        plan = sources_manager.plan_production(session, needs, now)
        store = ProductionStore(session, cid)
        factory = pipeline_factory or Pipeline
        pipe = factory(llm=llm, is_duplicate=store.is_duplicate, previous_captions=store.previous_captions,
                       post_render=store.post_render, on_clip_ready=store.on_clip_ready,
                       on_clip_failed=store.on_clip_failed,
                       performance_summary=lambda src: sources_manager.performance_summary(session, src))
        result = pipe.run(plan)
        produced = len(result.produced)
        stats.update(produced=produced, render_failed=len(result.failed),
                     source_errors=[(s.url, s.error) for s in result.source_errors][:20],
                     stage_seconds=result.stage_seconds)
        if result.blocked:
            raise_alert("YOUTUBE_BLOCKED", f"YouTube blocked downloads ({result.blocked}). Downloads stopped "
                        f"for this cycle — refresh cookies.txt / check the IP.", severity="CRITICAL",
                        session=session)
            stats["youtube_blocked"] = result.blocked
    # 4. assignment + schedule
    stats["assigned"] = assign_videos(session)
    try:
        import scheduler
        stats["scheduled"] = scheduler.schedule_all(session, now, llm=llm)
    except Exception as e:
        log.error("scheduling failed: %s\n%s", e, traceback.format_exc())
        stats["schedule_error"] = str(e)

    # 5. report for this cycle start (covers the previous cycle's results)
    stats["finished_at"] = utcnow().isoformat()
    try:
        import reports
        reports.build_cycle_report(session, cid, start - cycle_length(), start, extra=stats, llm=llm)
    except Exception as e:
        log.error("report failed: %s", e)
    system_state.put("last_cycle", json.dumps(stats, default=str), session)
    log.info("cycle #%d done: %s", idx, {k: stats.get(k) for k in ("produced", "assigned", "scheduled")})
    return stats


def main():
    import db_migrate
    import models
    import power
    import system_state

    db_migrate.run(log=log.info)
    power.prevent_sleep()
    while True:
        session = models.Session()
        try:
            stats = run_one_cycle(session)
            _, _, end = cycle_bounds(get_anchor(session), utcnow())
        except Exception as e:
            log.error("cycle crashed: %s\n%s", e, traceback.format_exc())
            end = utcnow() + _dt.timedelta(hours=1)
        finally:
            session.close()
        while utcnow() < end:
            system_state.heartbeat("cycle_runner")
            idle_jobs()
            time.sleep(300)


def idle_jobs():
    """Between cycles: the 6-hourly fast lane (new uploads of strong sources)."""
    import jobs
    import models
    s = models.Session()
    try:
        import sources_manager
        jobs.run_if_due("fast_lane", _dt.timedelta(hours=6), lambda: sources_manager.fast_lane(s), session=s)
    finally:
        s.close()


if __name__ == "__main__":
    main()
