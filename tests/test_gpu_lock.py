import os
import subprocess
import sys
import time

import pytest

from gpu_lock import GPUBusy, current_holder, gpu_job

HOLDER = """
import os, sys, time
sys.path.insert(0, {root!r})
os.environ['REELS_NO_DOTENV'] = '1'
os.environ['GPU_LOCK_PATH'] = {path!r}
os.environ['LOG_DIR'] = {logdir!r}
from gpu_lock import gpu_job
with gpu_job('other_process'):
    print('LOCKED', flush=True)
    time.sleep(4)
"""


def test_reentrant_and_owner_file():
    with gpu_job("outer"):
        assert current_holder()["job"] == "outer"
        with gpu_job("inner"):
            assert current_holder()["job"] == "outer"
        assert current_holder() is not None
    assert current_holder() is None


def test_other_process_blocks(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = HOLDER.format(root=root, path=os.environ["GPU_LOCK_PATH"], logdir=str(tmp_path))
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        for _ in range(10):                 # skip the logger's own stdout lines
            if p.stdout.readline().strip() == "LOCKED":
                break
        else:
            raise AssertionError("holder never locked")
        assert current_holder()["job"] == "other_process"
        with pytest.raises(GPUBusy):
            with gpu_job("me", timeout=0.5):
                pass
    finally:
        p.wait(timeout=20)
    with gpu_job("me", timeout=5):
        assert current_holder()["job"] == "me"
