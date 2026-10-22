"""A service started twice (watchdog race right after boot: last session's heartbeat looks stale) must not
run twice — full test I saw two cycle_runners run cycle #0 side by side."""
import os
import subprocess
import sys

import pytest

import system_state

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLDER = ("import sys; sys.path.insert(0, %r); import system_state; system_state.single_instance('svc_t'); "
          "print('held', flush=True); sys.stdin.read()" % ROOT)


def test_second_copy_of_a_service_exits_without_registering(db):
    holder = subprocess.Popen([sys.executable, "-c", HOLDER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True, env=dict(os.environ))
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(SystemExit) as e:
            system_state.register_service("svc_t", db)
        assert e.value.code == 0
        assert system_state.service_pid("svc_t", db) is None          # the running copy's PID is untouched
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
    system_state.register_service("svc_t", db)                        # lock released with the process
    assert system_state.service_pid("svc_t", db) == os.getpid()
    system_state.register_service("svc_t", db)                        # re-entrant inside one process
