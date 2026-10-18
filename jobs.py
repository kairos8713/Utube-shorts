"""
jobs.py — "run at most every N" helper backed by system_state (survives restarts).
"""
import datetime as _dt
import traceback

import system_state
from log_setup import get_logger
from timeutil import utcnow

log = get_logger("jobs")


def due(name: str, every: _dt.timedelta, now=None, session=None) -> bool:
    now = now or utcnow()
    last = system_state.get(f"job:{name}", None, session)
    if last:
        try:
            if now - _dt.datetime.fromisoformat(last) < every:
                return False
        except ValueError:
            pass
    return True


def mark(name: str, now=None, session=None):
    system_state.put(f"job:{name}", (now or utcnow()).isoformat(), session)


def run_if_due(name: str, every: _dt.timedelta, fn, now=None, session=None):
    if not due(name, every, now, session):
        return None
    try:
        result = fn()
    except Exception as e:
        log.error("job %s failed: %s\n%s", name, e, traceback.format_exc())
        result = e
    mark(name, now, session)
    return result
