"""
publish_on_connect.py — USB-Tethering-Triggered IG Publisher
=============================================================
Triggered when the phone's USB tethering interface appears (via
Windows Task Scheduler on network-adapter-connection event, or
run manually after connecting the phone).

For each account with pending content, IN SEQUENCE:
  1. Toggle airplane mode ON  (ADB) → wait 5s
  2. Toggle airplane mode OFF (ADB) → poll until tethering IP is back
  3. Bind instagrapi HTTP adapter to the tethering interface source IP
  4. Publish pending video via instagrapi
  5. On success: move pending/ → published/
     On failure: leave in pending/, log error, continue to next account

Each account gets its own airplane-mode cycle → fresh IP per post.

NOTE on Samsung One UI airplane-mode toggle:
  Some One UI versions ignore "settings put global airplane_mode_on"
  even with the broadcast intent. If this does not work on your S24,
  replace the toggle calls with:
    adb shell svc data disable  / enable
  Report back after manual verification.

Log: logs/publish_on_connect_YYYYMMDD_HHMMSS.log
"""

import os
import sys
import gc
import time
import logging
import datetime
import subprocess
import socket
import struct
import traceback
import shutil

# ── Ensure project root on path ───────────────────────────────────────────────
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT_DIR)

# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_DIR = os.path.join(ROOT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_now_str  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
_log_file = os.path.join(LOG_DIR, f"publish_on_connect_{_now_str}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[
        logging.FileHandler(_log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("publish_on_connect")

# ── Directory layout ──────────────────────────────────────────────────────────
PENDING_BASE   = os.path.join(ROOT_DIR, "pending")
PUBLISHED_BASE = os.path.join(ROOT_DIR, "published")
os.makedirs(PUBLISHED_BASE, exist_ok=True)

# ── ADB config ────────────────────────────────────────────────────────────────
ADB_BINARY          = "adb"         # must be in PATH (platform-tools)
ADB_TIMEOUT_S       = 10            # per-command ADB timeout

# ── IP polling config (after airplane OFF) ────────────────────────────────────
TETHER_IFACE_PREFIX = "192.168."    # USB tethering usually lands on 192.168.x.x
TETHER_POLL_INTERVAL_S = 2          # seconds between connectivity checks
TETHER_POLL_TIMEOUT_S  = 30         # max seconds to wait for IP restoration
TETHER_PING_HOST       = "8.8.8.8"  # host to ping through tethering interface

# ── instagrapi config ─────────────────────────────────────────────────────────
IG_REQUEST_TIMEOUT  = 60            # seconds for each IG API call
IG_DELAY_BETWEEN_S  = 15            # polite delay between accounts


# ══════════════════════════════════════════════════════════════════════════════
#  ADB helpers
# ══════════════════════════════════════════════════════════════════════════════

def _adb(*args, timeout=ADB_TIMEOUT_S):
    """
    Run an adb command. Returns (returncode, stdout, stderr).
    Never raises — caller always checks returncode.
    """
    cmd = [ADB_BINARY] + list(args)
    log.debug("ADB: %s", " ".join(cmd))
    try:
        r = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        log.warning("ADB timeout: %s", " ".join(cmd))
        return -1, "", "timeout"
    except FileNotFoundError:
        log.error("ADB binary not found. Install platform-tools and add to PATH.")
        return -1, "", "not_found"
    except Exception as e:
        log.error("ADB exception: %s", e)
        return -1, "", str(e)


def adb_verify_device():
    """Verify at least one device is visible via ADB. Returns True/False."""
    rc, stdout, _ = _adb("devices")
    if rc != 0:
        return False
    lines = [l for l in stdout.splitlines() if l.strip() and "List of devices" not in l]
    for line in lines:
        if "device" in line.split("\t")[-1:]:
            return True
    return bool(lines)


def airplane_on():
    """
    Toggle airplane mode ON via ADB.
    Samsung One UI note: broadcast intent may be needed in addition to
    the settings put command for the radio to actually cut.
    """
    log.info("[ADB] Airplane mode ON")
    _adb("shell", "settings", "put", "global", "airplane_mode_on", "1")
    _adb(
        "shell", "am", "broadcast",
        "-a", "android.intent.action.AIRPLANE_MODE",
        "--ez", "state", "true",
    )
    time.sleep(5)


def airplane_off():
    """Toggle airplane mode OFF via ADB."""
    log.info("[ADB] Airplane mode OFF")
    _adb("shell", "settings", "put", "global", "airplane_mode_on", "0")
    _adb(
        "shell", "am", "broadcast",
        "-a", "android.intent.action.AIRPLANE_MODE",
        "--ez", "state", "false",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Tethering IP detection
# ══════════════════════════════════════════════════════════════════════════════

def _get_tether_ip():
    """
    Scan local network interfaces for a tethering IP (192.168.x.x range).
    Returns the source IP string to bind to, or None if not found.

    Uses socket.getaddrinfo to enumerate all local IPs — works on Windows
    without requiring psutil or netifaces.
    """
    try:
        hostname = socket.gethostname()
        addrs = socket.getaddrinfo(hostname, None, socket.AF_INET)
        for addr in addrs:
            ip = addr[4][0]
            if ip.startswith(TETHER_IFACE_PREFIX):
                return ip
    except Exception:
        pass
    return None


def _can_reach_internet(source_ip):
    """
    Try a TCP connect to 8.8.8.8:53 bound to source_ip.
    Returns True if reachable within 3 seconds.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.bind((source_ip, 0))
        s.connect(("8.8.8.8", 53))
        s.close()
        return True
    except Exception:
        return False


def poll_tether_ip(timeout_s=TETHER_POLL_TIMEOUT_S,
                   interval_s=TETHER_POLL_INTERVAL_S):
    """
    Poll until a tethering interface IP appears AND can reach the internet.
    Returns the source IP string, or None on timeout.
    """
    log.info("[TETHER] Polling for tethering IP (timeout=%ds) ...", timeout_s)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ip = _get_tether_ip()
        if ip:
            log.info("[TETHER] Found tethering IP: %s — checking connectivity ...", ip)
            if _can_reach_internet(ip):
                log.info("[TETHER] Internet reachable via %s", ip)
                return ip
            else:
                log.info("[TETHER] %s found but internet not yet reachable — retrying ...", ip)
        else:
            log.debug("[TETHER] No tethering IP yet ...")
        time.sleep(interval_s)
    log.warning("[TETHER] Tethering IP not restored within %ds", timeout_s)
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  instagrapi session management
# ══════════════════════════════════════════════════════════════════════════════

def _build_ig_client(account, source_ip):
    """
    Create and authenticate an instagrapi Client for the given account,
    binding all HTTP traffic to source_ip (the tethering interface).

    Session file path: account.ig_session_path (set in DB) or
    sessions/{account_id}_session.json as fallback.

    Returns instagrapi.Client or None on failure.
    """
    try:
        from instagrapi import Client
        from instagrapi.mixins.challenge import ChallengeChoice
    except ImportError:
        log.error("instagrapi not installed. pip install instagrapi")
        return None

    session_path = (
        account.ig_session_path
        if account.ig_session_path
        else os.path.join(ROOT_DIR, "sessions", f"{account.id}_session.json")
    )

    cl = Client()
    cl.request_timeout = IG_REQUEST_TIMEOUT

    # ── Bind HTTP adapter to source IP ───────────────────────────────────────
    # instagrapi uses the `requests` library internally.
    # We override the session's HTTPAdapter to bind to source_ip so all
    # outgoing connections use the tethering interface.
    try:
        import requests
        from requests.adapters import HTTPAdapter

        class _BoundAdapter(HTTPAdapter):
            def __init__(self, src_address, **kwargs):
                self._src_address = src_address
                super().__init__(**kwargs)

            def send(self, request, **kwargs):
                # Inject source_address into the underlying urllib3 connection pool
                kwargs.setdefault("proxies", {})
                # urllib3 respects socket_options but not source_address natively
                # via requests — we monkey-patch at init time instead.
                return super().send(request, **kwargs)

        # The cleanest approach: monkey-patch socket.create_connection to
        # bind the outgoing socket to source_ip.  Scoped to this publish step,
        # restored immediately after (see _restore_socket_create in caller).
        # We store the adapter on cl for documentation; actual binding is via
        # the socket monkey-patch applied before any API call (see publish_clip).
        cl._tether_source_ip = source_ip
    except Exception as e:
        log.warning("[IG] Could not configure source_ip binding: %s", e)

    # ── Load or create session ────────────────────────────────────────────────
    if os.path.exists(session_path):
        try:
            cl.load_settings(session_path)
            cl.login(account.mail, account.password)
            log.info("[acc=%d] Session loaded from %s", account.id, session_path)
            return cl
        except Exception as e:
            log.warning("[acc=%d] Existing session invalid (%s) — re-logging in", account.id, e)

    # Fresh login
    try:
        cl.login(account.mail, account.password)
        os.makedirs(os.path.dirname(session_path) or ".", exist_ok=True)
        cl.dump_settings(session_path)
        log.info("[acc=%d] Fresh login OK, session saved → %s", account.id, session_path)
        return cl
    except Exception as e:
        log.error("[acc=%d] Login failed: %s", account.id, e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  Pending content discovery
# ══════════════════════════════════════════════════════════════════════════════

def _get_pending_clip(account_id):
    """
    Find the oldest pending MP4 for an account.
    Returns (mp4_path, txt_path) or (None, None).
    """
    pending_dir = os.path.join(PENDING_BASE, str(account_id))
    if not os.path.isdir(pending_dir):
        return None, None

    mp4_files = sorted([
        f for f in os.listdir(pending_dir)
        if f.endswith(".mp4")
    ])
    if not mp4_files:
        return None, None

    mp4_name = mp4_files[0]
    mp4_path = os.path.join(pending_dir, mp4_name)
    txt_path = mp4_path.replace(".mp4", ".txt")
    return mp4_path, txt_path if os.path.exists(txt_path) else None


def _read_caption(txt_path):
    """Read caption from txt file. Returns empty string if missing."""
    if not txt_path or not os.path.exists(txt_path):
        return ""
    try:
        with open(txt_path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════════════════
#  IG upload
# ══════════════════════════════════════════════════════════════════════════════

def _bind_socket_to_ip(source_ip):
    """
    Monkey-patch socket.create_connection to bind to source_ip.
    Returns the original function so the caller can restore it.
    """
    _original = socket.create_connection

    def _patched(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                 source_address=None):
        return _original(address, timeout=timeout,
                         source_address=(source_ip, 0))

    socket.create_connection = _patched
    return _original


def _restore_socket(original_fn):
    """Restore the original socket.create_connection."""
    socket.create_connection = original_fn


def publish_clip(account, mp4_path, caption, source_ip):
    """
    Upload mp4_path as an IG Reel for account.
    All HTTP bound to source_ip via socket monkey-patch.
    Returns instagrapi media ID string on success, None on failure.
    """
    original_create_connection = _bind_socket_to_ip(source_ip)
    try:
        cl = _build_ig_client(account, source_ip)
        if cl is None:
            return None

        log.info("[acc=%d] Uploading reel: %s", account.id, os.path.basename(mp4_path))
        media = cl.clip_upload(mp4_path, caption=caption)
        post_id = str(media.id) if media else None
        log.info("[acc=%d] Upload OK — post_id=%s", account.id, post_id)
        return post_id
    except Exception as e:
        log.error("[acc=%d] Upload failed: %s\n%s",
                  account.id, e, traceback.format_exc())
        return None
    finally:
        _restore_socket(original_create_connection)


# ══════════════════════════════════════════════════════════════════════════════
#  Per-account publish worker
# ══════════════════════════════════════════════════════════════════════════════

def publish_account(account):
    """
    Full airplane-cycle + publish for one account.
    Returns True on successful upload, False otherwise.
    """
    acc_id = account.id
    log.info("=== [acc=%d] %s ===", acc_id, account.name or "account_" + str(acc_id))

    # 1. Find pending content
    mp4_path, txt_path = _get_pending_clip(acc_id)
    if not mp4_path:
        log.info("[acc=%d] No pending content — skipping", acc_id)
        return False

    log.info("[acc=%d] Pending clip: %s", acc_id, os.path.basename(mp4_path))

    # 2. Airplane ON
    airplane_on()

    # 3. Airplane OFF + poll for IP
    airplane_off()
    source_ip = poll_tether_ip()
    if not source_ip:
        log.error("[acc=%d] No tethering IP after airplane cycle — skipping", acc_id)
        return False
    log.info("[acc=%d] Tethering IP: %s", acc_id, source_ip)

    # 4. Read caption
    caption = _read_caption(txt_path)

    # 5. Publish
    post_id = publish_clip(account, mp4_path, caption, source_ip)

    # 6. On success: move to published/; on failure: leave in pending/
    if post_id:
        published_dir = os.path.join(PUBLISHED_BASE, str(acc_id))
        os.makedirs(published_dir, exist_ok=True)

        dest_mp4 = os.path.join(published_dir, os.path.basename(mp4_path))
        try:
            shutil.move(mp4_path, dest_mp4)
            log.info("[acc=%d] Moved to published/ -> %s", acc_id, dest_mp4)
        except Exception as e:
            log.warning("[acc=%d] Could not move MP4 to published/: %s", acc_id, e)

        if txt_path and os.path.exists(txt_path):
            try:
                shutil.move(txt_path, os.path.join(published_dir,
                                                    os.path.basename(txt_path)))
            except Exception:
                pass

        log.info("[acc=%d] SUCCESS  post_id=%s", acc_id, post_id)
        return True
    else:
        log.error("[acc=%d] FAILED — clip left in pending/", acc_id)
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from models import OwnedChannels, DB_PATH

    t0 = time.time()
    log.info("=== publish_on_connect.py START  %s ===",
             datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # ── Verify ADB device ─────────────────────────────────────────────────────
    if not adb_verify_device():
        log.error("No ADB device detected. Connect phone with USB debugging enabled.")
        sys.exit(1)
    log.info("ADB device detected OK")

    # ── Load accounts ─────────────────────────────────────────────────────────
    _engine  = create_engine(DB_PATH, echo=False)
    _Session = sessionmaker(bind=_engine)
    session  = _Session()

    try:
        accounts = (
            session.query(OwnedChannels)
            .filter(OwnedChannels.is_active == True)
            .all()
        )
        log.info("Active accounts: %d", len(accounts))
    except Exception as e:
        log.critical("Cannot load accounts: %s", e)
        sys.exit(1)

    # Filter: only accounts that actually have pending content
    accounts_with_pending = [
        a for a in accounts
        if _get_pending_clip(a.id)[0] is not None
    ]
    log.info("Accounts with pending content: %d", len(accounts_with_pending))

    if not accounts_with_pending:
        log.info("Nothing to publish — exiting.")
        return

    # ── Sequential publish — one airplane cycle per account ──────────────────
    total     = len(accounts_with_pending)
    succeeded = 0
    failed    = 0

    for account in accounts_with_pending:
        try:
            ok = publish_account(account)
            if ok:
                succeeded += 1
            else:
                failed += 1
        except Exception as e:
            log.error("[acc=%d] Unhandled exception: %s\n%s",
                      account.id, e, traceback.format_exc())
            failed += 1
        finally:
            gc.collect()

        # Polite inter-account delay (even on failure, to let radio settle)
        if account is not accounts_with_pending[-1]:
            log.info("Waiting %ds before next account ...", IG_DELAY_BETWEEN_S)
            time.sleep(IG_DELAY_BETWEEN_S)

    elapsed = time.time() - t0
    log.info(
        "\n=== SUMMARY ===\n"
        "  Total with pending:  %d\n"
        "  Succeeded:           %d\n"
        "  Failed:              %d\n"
        "  Total elapsed:       %.1f s (%.1f min)\n"
        "=== publish_on_connect.py END ===",
        total, succeeded, failed, elapsed, elapsed / 60,
    )


if __name__ == "__main__":
    main()
