import os
import sys
import threading
import time

# Add project root to path so all modules resolve correctly
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web import create_app

# ── Flask Application ────────────────────────────────────────────────────────
app = create_app()

# ── Shared background state ──────────────────────────────────────────────────
STATS_INTERVAL_HOURS = 6

_fulfillment_last_run: float = 0.0   # Unix timestamp of last fulfillment check
_fulfillment_snapshot: list  = []    # cached per-channel inventory; read by API


# ── Fulfillment Background Thread (6-hour interval) ─────────────────────────

def _fulfillment_worker():
    """
    Runs the Production Fulfillment Loop every 6 hours.
    Caches the inventory snapshot in _fulfillment_snapshot for the
    /api/fulfillment_status endpoint.
    """
    global _fulfillment_last_run, _fulfillment_snapshot

    time.sleep(40)

    while True:
        try:
            from fulfillment_engine import run_fulfillment_check
            print("[FULFILL-BG] Running fulfillment inventory check ...")
            snapshot = run_fulfillment_check()
            _fulfillment_snapshot = snapshot
            _fulfillment_last_run = time.time()
            gaps = sum(i["gap"] for i in snapshot)
            print(
                f"[FULFILL-BG] Done. {len(snapshot)} channel(s) checked, "
                f"total gap={gaps}. Next run in {STATS_INTERVAL_HOURS}h."
            )
        except Exception as e:
            print(f"[FULFILL-BG] Error: {e}")

        time.sleep(STATS_INTERVAL_HOURS * 3600)


# ── Background Service Launcher ──────────────────────────────────────────────

def start_background_services():
    """Starts all long-running background services."""

    # ── YouTube Analytics API scheduler (every 30 min) ───────────────────────
    try:
        from youtube_analytics import start_scheduler as start_analytics_scheduler
        start_analytics_scheduler(interval_minutes=30)
        print("[INFO] YouTube Analytics scheduler started (30min)")
    except Exception as e:
        print(f"[WARN] Analytics scheduler failed to start: {e}")

    # ── Fulfillment thread ───────────────────────────────────────────────────
    # fulfill_thread = threading.Thread(target=_fulfillment_worker, daemon=True,
    #                                   name="FulfillmentWorker")
    # fulfill_thread.start()
    # print("[INFO] Fulfillment background worker started (interval: 6h, offset: +30s).")


if __name__ == "__main__":
    start_background_services()
    print("[INFO] Dashboard starting on http://0.0.0.0:5050 ...")
    app.run(debug=True, use_reloader=False, host='0.0.0.0', port=5050)