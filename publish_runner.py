"""
publish_runner.py — the publishing service (separate process).

Every minute:
  * heartbeat; nothing external while the emergency stop is on
  * missed-post redistribution (every 10 min)
  * publish due queue items (at most one per tick — the 8-minute global gap
    makes more pointless); deferred items move to gap + 1–3 random minutes
Periodic: token refresh (daily), insights collection (30 min, analytics),
maintenance (daily). The fast lane (GPU production) runs in cycle_runner.
"""
import datetime as _dt
import random
import time
import traceback

import guardrails
import jobs
import scheduler
import system_state
from instagram import publisher, token_refresh
from log_setup import get_logger
from models import Session, UploadQueue
from timeutil import utcnow

log = get_logger("publish_runner")

TICK_S = 60


def publish_due(session, now=None, rng=None, **kw) -> list:
    now = now or utcnow()
    rng = rng or random.Random()
    outcomes = []
    if system_state.emergency_stopped(session):
        return outcomes
    due = (session.query(UploadQueue).filter(UploadQueue.status == "PENDING", UploadQueue.scheduled_for <= now)
           .order_by(UploadQueue.priority.desc(), UploadQueue.scheduled_for).all())
    for item in due:
        outcome = publisher.publish_item(session, item, now=now, **kw)
        outcomes.append((item.id, outcome))
        if outcome == "DEFER_GAP":
            last = guardrails.last_global_post_time(session) or now
            item.scheduled_for = max(now, last + guardrails.MIN_GAP_BETWEEN_POSTS) + \
                _dt.timedelta(seconds=rng.randint(60, 180))
            session.commit()
            continue
        if outcome in ("PUBLISHED", "RETRY", "RETRY_SLOW", "UNCERTAIN", "PAUSED", "COPYRIGHT"):
            break           # the gap now applies to everyone
    return outcomes


def tick(session, now=None):
    now = now or utcnow()
    system_state.heartbeat("publisher", session)
    jobs.run_if_due("reconcile_uncertain", _dt.timedelta(minutes=10),
                    lambda: publisher.reconcile_uncertain(session, now), now, session)
    jobs.run_if_due("reschedule_missed", _dt.timedelta(minutes=10),
                    lambda: scheduler.reschedule_missed(session, now), now, session)
    publish_due(session, now)
    jobs.run_if_due("token_refresh", _dt.timedelta(hours=24), lambda: token_refresh.run(session, now), now, session)
    try:
        import analytics
        if hasattr(analytics, "collect_due"):
            jobs.run_if_due("insights", _dt.timedelta(minutes=30), lambda: analytics.collect_due(session, now),
                            now, session)
    except ImportError:
        pass
    try:
        import maintenance
        jobs.run_if_due("maintenance", _dt.timedelta(hours=24), lambda: maintenance.daily(session, now), now, session)
    except ImportError:
        pass


def startup_reconcile(session, now=None, **kw) -> dict:
    """Settle posts left CONTAINER / UNKNOWN (e.g. by a crash) before publishing anything again."""
    out = publisher.reconcile_uncertain(session, now, **kw)
    jobs.mark("reconcile_uncertain", now, session)
    log.info("start-up reconcile: %s", out)
    return out


def main():
    import db_migrate
    import power
    db_migrate.run(log=log.info)
    power.prevent_sleep()
    system_state.register_service("publisher")
    log.info("publisher service started (DRY_RUN=%s)", __import__("config").settings.DRY_RUN)
    s = Session()
    try:
        startup_reconcile(s)
    except Exception as e:
        log.error("start-up reconcile failed: %s", e)
    finally:
        s.close()
    while True:
        session = Session()
        try:
            tick(session)
        except Exception as e:
            log.error("tick failed: %s\n%s", e, traceback.format_exc())
        finally:
            session.close()
        time.sleep(TICK_S)


if __name__ == "__main__":
    main()
