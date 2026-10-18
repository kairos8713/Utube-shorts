"""
timeutil.py — all stored/compared times are naive UTC; Europe/Warsaw is only
used to define posting windows/slots and for display. zoneinfo handles DST
(e.g. 25 Oct 2026 03:00 CEST → 02:00 CET).

Posting window default 15:00–05:00 local. 2-hour slots are keyed by their local
start hour: 15, 17, 19, 21, 23, 1, 3.
"""
import datetime as _dt
from zoneinfo import ZoneInfo

from config import settings

UTC = _dt.timezone.utc


def tz() -> ZoneInfo:
    return ZoneInfo(settings.DISPLAY_TZ)


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(UTC).replace(tzinfo=None)


def to_local(dt_utc: _dt.datetime) -> _dt.datetime:
    if dt_utc is None:
        return None
    return dt_utc.replace(tzinfo=UTC).astimezone(tz())


def fmt_local(dt_utc, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return to_local(dt_utc).strftime(fmt) if dt_utc else "—"


def local_to_utc(local_naive: _dt.datetime) -> _dt.datetime:
    """Local wall time → naive UTC. Ambiguous times use the first occurrence (fold=0);
    non-existent (spring-forward gap) times are shifted forward by the gap."""
    aware = local_naive.replace(tzinfo=tz())
    back = aware.astimezone(UTC).astimezone(tz()).replace(tzinfo=None)
    if back != local_naive:                       # inside the spring-forward gap
        aware = (local_naive + _dt.timedelta(hours=1)).replace(tzinfo=tz())
    return aware.astimezone(UTC).replace(tzinfo=None)


def parse_hhmm(s: str) -> _dt.time:
    h, m = s.strip().split(":")
    return _dt.time(int(h), int(m))


def parse_window(spec: str = None):
    spec = spec or f"{settings.POSTING_WINDOW_START}-{settings.POSTING_WINDOW_END}"
    a, b = spec.split("-")
    return parse_hhmm(a), parse_hhmm(b)


def window_for(local_day: _dt.date, spec: str = None):
    """(utc_start, utc_end) of the posting window that STARTS on local_day."""
    start_t, end_t = parse_window(spec)
    start_local = _dt.datetime.combine(local_day, start_t)
    end_day = local_day + _dt.timedelta(days=1) if end_t <= start_t else local_day
    end_local = _dt.datetime.combine(end_day, end_t)
    return local_to_utc(start_local), local_to_utc(end_local)


def window_day_of(dt_utc: _dt.datetime, spec: str = None):
    """Local day whose posting window contains dt_utc, or None if outside any window."""
    loc = to_local(dt_utc).date()
    for day in (loc - _dt.timedelta(days=1), loc):
        s, e = window_for(day, spec)
        if s <= dt_utc < e:
            return day
    return None


def slots_for(local_day: _dt.date, spec: str = None, slot_hours: int = 2):
    """[(slot_key, utc_start, utc_end)] — 2-hour local slots inside the window."""
    start_t, end_t = parse_window(spec)
    w_start, w_end = window_for(local_day, spec)
    out = []
    cur_local = _dt.datetime.combine(local_day, start_t)
    while True:
        s_utc = local_to_utc(cur_local)
        nxt_local = cur_local + _dt.timedelta(hours=slot_hours)
        e_utc = min(local_to_utc(nxt_local), w_end)
        if s_utc >= w_end:
            break
        out.append((cur_local.hour, s_utc, e_utc))
        cur_local = nxt_local
    return out


def slot_key_of(dt_utc: _dt.datetime, spec: str = None):
    day = window_day_of(dt_utc, spec)
    if day is None:
        return None
    for key, s, e in slots_for(day, spec):
        if s <= dt_utc < e:
            return key
    return None
