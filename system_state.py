"""
system_state.py — global key/value state in the DB.

* emergency_stop: when "true", NO external action (publish, reply, hide) may
  happen. Every job calls `assert_not_stopped()` immediately before acting.
  Only Emre (panel) sets/clears it; nothing in brain/ can.
* heartbeats: each service writes heartbeat:<name> = UTC ISO time.
"""
import datetime as _dt
import json

from models import Session, SystemState
from timeutil import utcnow

EMERGENCY_KEY = "emergency_stop"


class EmergencyStop(RuntimeError):
    pass


def get(key: str, default=None, session=None):
    own = session is None
    s = session or Session()
    try:
        row = s.get(SystemState, key)
        return row.value if row else default
    finally:
        if own:
            s.close()


def put(key: str, value, session=None):
    own = session is None
    s = session or Session()
    try:
        row = s.get(SystemState, key)
        if row is None:
            row = SystemState(key=key)
            s.add(row)
        row.value = value if isinstance(value, str) or value is None else json.dumps(value)
        row.updated_at = utcnow()
        s.commit()
    finally:
        if own:
            s.close()


def emergency_stopped(session=None) -> bool:
    return (get(EMERGENCY_KEY, "false", session) or "false").lower() == "true"


def assert_not_stopped(session=None):
    if emergency_stopped(session):
        raise EmergencyStop("emergency stop is active — external actions are blocked")


def set_emergency_stop(on: bool, by: str = "panel", session=None):
    put(EMERGENCY_KEY, "true" if on else "false", session)
    put("emergency_stop_changed", json.dumps({"on": on, "by": by, "at": utcnow().isoformat()}), session)


def heartbeat(service: str, session=None):
    put(f"heartbeat:{service}", utcnow().isoformat(), session)


def register_service(service: str, session=None):
    """Called once at service start: PID for the watchdog + first heartbeat."""
    import os
    put(f"pid:{service}", str(os.getpid()), session)
    heartbeat(service, session)


def service_pid(service: str, session=None):
    v = get(f"pid:{service}", None, session)
    return int(v) if v and v.isdigit() else None


def last_heartbeat(service: str, session=None):
    v = get(f"heartbeat:{service}", None, session)
    try:
        return _dt.datetime.fromisoformat(v) if v else None
    except ValueError:
        return None
