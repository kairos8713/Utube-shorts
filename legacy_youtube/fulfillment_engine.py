"""
fulfillment_engine.py
=====================
Production Fulfillment Loop — Inventory Checker & Subprocess Dispatcher
=======================================================================

Responsibilities:
  1. For every OwnedChannel, compute how many videos are needed in the next 7
     days (target) vs how many are already queued/ready (current).
  2. If gap > 0, enqueue a fulfillment job.
  3. A single-consumer thread drains the queue ONE channel at a time, launching
        python sector_processor.py --fulfillment --channel-id N --gap N
     as a subprocess and waiting for it to finish before starting the next one.

GPU serialisation (SINGLE-GPU MODE – cuda:0 only):
  Only ONE sector_processor.py subprocess runs at a time so the single
  cuda:0 card (Qwen + Kokoro + Whisper + NVENC) is never double-booked.

This module is intentionally import-light — it never loads Qwen / Whisper /
Kokoro. All heavy AI work happens inside the subprocess.
"""

import os
import sys
import time
import queue
import threading
import datetime
import subprocess

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Ensure project root is on path regardless of working directory
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from models import DB_PATH, OwnedChannels, Sectors, videos, UploadQueue, logs

# ─── DB Setup (lightweight, no GPU models) ──────────────────────────────────
engine  = create_engine(DB_PATH)
Session = sessionmaker(bind=engine)

# ─── Config ─────────────────────────────────────────────────────────────────
DAYS_WINDOW  = 7           # planning horizon (weekly cycle)
SUBPROCESS_TIMEOUT = 480 * 60  # 8 hours in seconds

LEVEL_QUOTAS = {           # mirrors LEVEL_CONFIG in sector_processor.py
    1: 1,
    2: 2,
    3: 3,
    4: 5,
    5: 7,
}

# ─── Module-level state (shared with web/app.py reader thread) ──────────────
_snapshot: list   = []          # last inventory scan result
_last_run: float  = 0.0         # Unix timestamp of last check
_work_queue       = None        # threading.Queue, initialised once
_consumer_thread  = None        # single GPU-serialised consumer
_processing_channels: set = set()   # channels currently running in subprocess


# ═══════════════════════════════════════════════════════════════════════════
#  INVENTORY
# ═══════════════════════════════════════════════════════════════════════════

def get_channel_inventory(sess, ch: OwnedChannels) -> dict:
    """
    Returns a full pipeline snapshot for a single channel:

      target    = level_quota × 7
      pending   = PENDING/UPLOADED UploadQueue slots in the next-7-day window
      ready     = videos in the channel's sector that are NOT yet in any queue
                  entry for this channel  (available to schedule)
      current   = pending + ready
      gap       = max(0, target – current)
      status    = "FULL" / "LOW" / "CRITICAL"
    """
    daily  = LEVEL_QUOTAS.get(ch.level or 1, 1)
    target = daily * DAYS_WINDOW

    # All PENDING queue slots for this channel (no date-window filter —
    # scheduled dates beyond 7 days are still valid inventory)
    pending = sess.query(UploadQueue).filter(
        UploadQueue.owned_channel_id == ch.id,
        UploadQueue.status.in_(["PENDING"]),
    ).count()

    # Videos owned by THIS channel that haven't been claimed by its queue
    claimed_ids = [
        uq.video_id for uq in
        sess.query(UploadQueue.video_id)
            .filter_by(owned_channel_id=ch.id)
            .all()
        if uq.video_id is not None
    ]
    ready_q = sess.query(videos).filter(videos.owned_channel_id == ch.id, videos.is_used == False)
    if claimed_ids:
        ready_q = ready_q.filter(~videos.id.in_(claimed_ids))
    ready = ready_q.count()

    # Use pending as primary metric; fall back to unclaimed ready count
    # when the queue is empty but rendered videos exist
    current = pending if pending > 0 else ready
    gap     = max(0, target - current)

    if gap == 0:
        status = "FULL"
    elif gap <= target * 0.30:
        status = "LOW"
    else:
        status = "CRITICAL"

    return {
        "channel_id":  ch.id,
        "name":        ch.name or f"Channel {ch.id}",
        "level":       ch.level or 1,
        "daily_quota": daily,
        "target":      target,
        "pending":     pending,
        "ready":       ready,
        "current":     current,
        "gap":         gap,
        "status":      status,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  POOL EXHAUSTION WARNINGS
# ═══════════════════════════════════════════════════════════════════════════

def get_pool_exhaustion_warnings(limit: int = 10) -> list:
    """
    Returns the last `limit` SOURCE_POOL_EXHAUSTED log entries for the
    dashboard warnings panel.
    """
    sess = Session()
    try:
        rows = (
            sess.query(logs)
            .filter(logs.messages.like("SOURCE_POOL_EXHAUSTED%"))
            .order_by(logs.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "channel_id": r.owned_channel_id,
                "sector_id":  r.sector_id,
                "message":    r.messages,
                "timestamp":  r.uploaded_at.strftime("%Y-%m-%d %H:%M UTC")
                              if r.uploaded_at else "",
            }
            for r in rows
        ]
    finally:
        sess.close()


# ═══════════════════════════════════════════════════════════════════════════
#  FULFILLMENT CHECK (called by background worker every 6h)
# ═══════════════════════════════════════════════════════════════════════════

def run_fulfillment_check() -> list:
    """
    Scans all OwnedChannels, computes inventory gaps, and enqueues
    fulfillment jobs for any channel with gap > 0.

    Returns the full inventory snapshot (list of dicts) so the caller
    can cache it for the /api/fulfillment_status endpoint.
    """
    global _snapshot, _last_run

    sess     = Session()
    channels = sess.query(OwnedChannels).all()
    snapshot = []

    print(f"\n[FULFILL] Inventory check — {len(channels)} channel(s)")
    print(f"  {'Channel':<28} {'Lv':<4} {'Have':>6}/{'{Target}':<8} {'Gap':>6}  Status")
    print("  " + "─" * 62)

    for ch in channels:
        inv = get_channel_inventory(sess, ch)
        snapshot.append(inv)

        icon = {"FULL": "🟢", "LOW": "🟡", "CRITICAL": "🔴"}.get(inv["status"], "⚪")
        print(
            f"  {icon} {inv['name']:<27} {inv['level']:<4} "
            f"{inv['current']:>6}/{inv['target']:<8} {inv['gap']:>6}  {inv['status']}"
        )

        if inv["gap"] > 0:
            _enqueue_fulfillment(ch.id, inv["gap"])

    sess.close()

    _snapshot  = snapshot
    _last_run  = time.time()
    return snapshot


# ═══════════════════════════════════════════════════════════════════════════
#  SUBPROCESS QUEUE  (GPU-serialised consumer)
# ═══════════════════════════════════════════════════════════════════════════

def _enqueue_fulfillment(channel_id: int, gap: int):
    """Adds a (channel_id, gap) job to the serialised work queue."""
    global _work_queue, _consumer_thread

    if _work_queue is None:
        _work_queue = queue.Queue()
        _consumer_thread = threading.Thread(
            target=_consumer_loop,
            daemon=True,
            name="FulfillmentConsumer",
        )
        _consumer_thread.start()
        print("[FULFILL] GPU serialisation consumer started.")

    if channel_id in _processing_channels:
        print(f"[FULFILL] Channel #{channel_id} already processing — skipping duplicate enqueue.")
        return

    print(f"[FULFILL] Queuing: channel #{channel_id}  gap={gap}")
    _work_queue.put((channel_id, gap))


def _consumer_loop():
    """
    Single-threaded consumer. Processes ONE channel at a time so
    GPU 0 and GPU 1 are never double-booked between channels.
    """
    while True:
        channel_id, gap = _work_queue.get()
        try:
            _run_subprocess(channel_id, gap)
        except Exception as e:
            print(f"[FULFILL] Subprocess error (channel #{channel_id}): {e}")
        finally:
            _work_queue.task_done()


def _run_subprocess(channel_id: int, gap: int):
    """
    Launches sector_processor.py in fulfillment mode and BLOCKS until it
    finishes. This is the GPU serialisation choke-point.

    The subprocess inherits the same Python environment (venv/conda) as the
    Flask process via sys.executable.
    """
    script = os.path.join(_ROOT, "sector_processor.py")
    cmd    = [
        sys.executable, script,
        "--fulfillment",
        "--channel-id", str(channel_id),
        "--gap",        str(gap),
    ]

    print(f"\n[FULFILL] Launching subprocess: {' '.join(cmd)}")
    start = time.time()

    _processing_channels.add(channel_id)
    try:
        result = subprocess.run(
            cmd,
            timeout=SUBPROCESS_TIMEOUT,
            cwd=_ROOT,
        )
        elapsed = (time.time() - start) / 60
        print(
            f"[FULFILL] Subprocess finished: channel #{channel_id}  "
            f"rc={result.returncode}  elapsed={elapsed:.1f}m"
        )
    except subprocess.TimeoutExpired:
        print(
            f"[FULFILL] Subprocess TIMEOUT after {SUBPROCESS_TIMEOUT//60}m "
            f"for channel #{channel_id} — killed."
        )
    finally:
        _processing_channels.discard(channel_id)


# ═══════════════════════════════════════════════════════════════════════════
#  STANDALONE DRY-RUN  (no subprocess launched)
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "═" * 65)
    print("  Fulfillment Engine — Dry-Run Inventory Check")
    print("  (No subprocesses will be launched)")
    print("═" * 65)

    snap = []
    sess = Session()
    chs  = sess.query(OwnedChannels).all()

    if not chs:
        print("\n  No OwnedChannels found. Add channels via the dashboard.\n")
        sys.exit(0)

    print(f"\n  {'Channel':<30} {'Lv':<4} {'Have':>6}/{'Target':<8} {'Gap':>6}  Status")
    print("  " + "─" * 64)

    for ch in chs:
        inv  = get_channel_inventory(sess, ch)
        snap.append(inv)
        icon = {"FULL": "🟢", "LOW": "🟡", "CRITICAL": "🔴"}.get(inv["status"], "⚪")
        bar_filled = int((inv["current"] / max(inv["target"], 1)) * 20)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        print(
            f"  {icon} {inv['name']:<29} {inv['level']:<4} "
            f"{inv['current']:>6}/{inv['target']:<8} {inv['gap']:>6}  "
            f"[{bar}] {inv['status']}"
        )

    sess.close()

    total_gap = sum(i["gap"] for i in snap)
    print(f"\n  Total gap across all channels: {total_gap} videos")
    warnings = get_pool_exhaustion_warnings()
    if warnings:
        print(f"\n  ⚠  Pool Exhaustion Warnings ({len(warnings)}):")
        for w in warnings:
            print(f"    [{w['timestamp']}] {w['message'][:100]}")
    print()
