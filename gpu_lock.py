"""
gpu_lock.py — one GPU job at a time across ALL processes on this PC.

Whisper, Qwen (Ollama), render (NVENC) and RIFE take this lock. The fast lane,
the normal cycle and the comment responder therefore never hit the GPUs at the
same time. The long-form pipeline (utube-lfv / llama-server) must use the same
file: GPU_LOCK_PATH in .env (default ~/gpu_shared.lock).

    with gpu_job("whisper"):
        ...

A side-car JSON file (<lock>.owner) records the current holder for the panel.
The lock is re-entrant inside one process (nested gpu_job calls are fine).
"""
import json
import os
import threading
import time
from contextlib import contextmanager

from filelock import FileLock, Timeout

from config import settings
from log_setup import get_logger

log = get_logger("gpu_lock")

_locks = {}
_guard = threading.Lock()


class GPUBusy(TimeoutError):
    pass


def _lock_for(path: str) -> FileLock:
    with _guard:
        if path not in _locks:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            _locks[path] = FileLock(path)
        return _locks[path]


def _owner_path(path: str) -> str:
    return path + ".owner"


@contextmanager
def gpu_job(name: str, timeout: float = None):
    path = settings.GPU_LOCK_PATH
    lock = _lock_for(path)
    wait = settings.GPU_LOCK_TIMEOUT_S if timeout is None else timeout
    t0 = time.time()
    outermost = not lock.is_locked
    try:
        lock.acquire(timeout=wait)
    except Timeout as e:
        raise GPUBusy(f"GPU busy (holder={current_holder()}) after {wait}s waiting for {name}") from e
    if outermost:
        try:
            with open(_owner_path(path), "w", encoding="utf-8") as f:
                json.dump({"job": name, "pid": os.getpid(), "since": time.time()}, f)
        except OSError:
            pass
        log.info("GPU lock acquired by %s (waited %.1fs)", name, time.time() - t0)
    try:
        yield
    finally:
        if lock.lock_counter == 1:
            try:
                os.remove(_owner_path(path))
            except OSError:
                pass
            log.info("GPU lock released by %s after %.1fs", name, time.time() - t0)
        lock.release()


def current_holder():
    try:
        with open(_owner_path(settings.GPU_LOCK_PATH), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
