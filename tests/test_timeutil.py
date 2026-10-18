import datetime as dt

import timeutil as tu

D = dt.datetime


def test_normal_day_window_is_14h():
    s, e = tu.window_for(dt.date(2026, 10, 20))
    assert s == D(2026, 10, 20, 13, 0) and e == D(2026, 10, 21, 3, 0)


def test_dst_end_2026_10_25_window_is_15h():
    # Window starting Sat 24 Oct 15:00 CEST ends Sun 25 Oct 05:00 CET (clocks go back at 03:00).
    s, e = tu.window_for(dt.date(2026, 10, 24))
    assert s == D(2026, 10, 24, 13, 0)
    assert e == D(2026, 10, 25, 4, 0)
    assert (e - s) == dt.timedelta(hours=15)


def test_dst_slots_keep_local_keys():
    slots = tu.slots_for(dt.date(2026, 10, 24))
    assert [k for k, _, _ in slots] == [15, 17, 19, 21, 23, 1, 3]
    by = {k: (s, e) for k, s, e in slots}
    assert by[1] == (D(2026, 10, 24, 23, 0), D(2026, 10, 25, 2, 0))     # 3 real hours
    assert by[3] == (D(2026, 10, 25, 2, 0), D(2026, 10, 25, 4, 0))
    # slots tile the window without gaps
    for (k1, s1, e1), (k2, s2, e2) in zip(slots, slots[1:]):
        assert e1 == s2


def test_window_after_dst_uses_cet():
    s, _ = tu.window_for(dt.date(2026, 10, 25))
    assert s == D(2026, 10, 25, 14, 0)


def test_ambiguous_and_gap_local_times():
    assert tu.local_to_utc(D(2026, 10, 25, 2, 30)) == D(2026, 10, 25, 0, 30)    # first occurrence
    assert tu.local_to_utc(D(2026, 3, 29, 2, 30)) == D(2026, 3, 29, 1, 30)      # spring-forward gap


def test_slot_key_and_window_day():
    assert tu.slot_key_of(D(2026, 10, 24, 23, 30)) == 1          # 01:30 CEST
    assert tu.slot_key_of(D(2026, 10, 25, 1, 30)) == 1           # 02:30 CET (second pass) still slot 1
    assert tu.window_day_of(D(2026, 10, 25, 3, 59)) == dt.date(2026, 10, 24)
    assert tu.window_day_of(D(2026, 10, 25, 4, 0)) is None       # 05:00 CET → outside window
    assert tu.fmt_local(D(2026, 10, 24, 13, 0)) == "2026-10-24 15:00"
