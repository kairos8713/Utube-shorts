"""
debug_pool.py  –  Pool Channel Diagnostic Tool
================================================
Run this BEFORE sector_processor.py to understand exactly why your pool
channels are not producing videos.

Usage:
    python debug_pool.py                  # scan all sectors
    python debug_pool.py --sector-id 2   # scan one sector
    python debug_pool.py --flush          # !! WIPE processed_source_videos table (fresh start)

This never touches the GPU — it only does yt-dlp fetches and DB reads.
"""

import os, sys, json, subprocess, argparse, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from models import DB_PATH, Sectors, Channels, OwnedChannels, ProcessedSourceVideos

engine  = create_engine(DB_PATH)
Session = sessionmaker(bind=engine)
sess    = Session()

SEP = "─" * 70


def check_processed_table():
    """Print summary of what's in processed_source_videos."""
    total   = sess.execute(text("SELECT COUNT(*) FROM processed_source_videos")).scalar()
    success = sess.execute(text("SELECT COUNT(*) FROM processed_source_videos WHERE status='PROCESSED_SUCCESS'")).scalar()
    failed  = sess.execute(text("SELECT COUNT(*) FROM processed_source_videos WHERE status LIKE 'FAILED%'")).scalar()
    no_clip = sess.execute(text("SELECT COUNT(*) FROM processed_source_videos WHERE status='PROCESSED_NO_CLIPS'")).scalar()

    print(f"\n{'═'*70}")
    print(f"  processed_source_videos table:")
    print(f"    Total rows         : {total}")
    print(f"    PROCESSED_SUCCESS  : {success}")
    print(f"    PROCESSED_NO_CLIPS : {no_clip}  ← (videos that downloaded but Qwen found 0 clips)")
    print(f"    FAILED_*           : {failed}")
    print(f"{'═'*70}")

    if no_clip > 0:
        print(f"\n  ⚠  WARNING: {no_clip} videos are marked PROCESSED_NO_CLIPS.")
        print(f"     These will NEVER be retried. If Qwen was misconfigured at the time,")
        print(f"     they are permanently skipped. Use --flush to reset.\n")


def fetch_raw_urls(channel_url: str, max_videos: int = 20) -> list:
    """Run yt-dlp and return raw entries list (no filtering)."""
    base_url = channel_url.rstrip("/")
    for suffix in ["/shorts", "/featured", "/videos", "/streams", "/about"]:
        base_url = base_url.split(suffix)[0]
    videos_tab = base_url + "/videos"

    cmd = (
        f'yt-dlp --flat-playlist --dump-single-json '
        f'--playlist-end {max_videos} "{videos_tab}"'
    )
    print(f"  → Running: {cmd}")
    proc = subprocess.run(
        cmd, shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
    )
    if proc.returncode != 0:
        stderr_msg = proc.stderr.decode("utf-8", errors="replace").strip()[:400]
        print(f"  ✗ yt-dlp error (rc={proc.returncode}):\n    {stderr_msg}")
        return []
    try:
        data = json.loads(proc.stdout)
        return data.get("entries") or []
    except Exception as e:
        print(f"  ✗ JSON parse error: {e}")
        return []


def diagnose_channel(ch: Channels):
    """Check a single pool channel and show exactly what's happening."""
    print(f"\n{SEP}")
    print(f"  Channel #{ch.id}: {ch.url}")
    print(SEP)

    entries = fetch_raw_urls(ch.url, max_videos=20)
    if not entries:
        print("  ✗ yt-dlp returned 0 entries. Check URL or bot protection.")
        return

    print(f"  yt-dlp found {len(entries)} entries (top 20). Details:\n")

    header = f"  {'#':<3}  {'Video ID':<14}  {'Duration':>8}  {'upload_date':<12}  {'Status'}"
    print(header)
    print(f"  {'─'*60}")

    for i, entry in enumerate(entries):
        if not entry:
            print(f"  {i+1:<3}  (null entry)")
            continue

        vid_id   = entry.get("id", "???")
        vid_url  = f"https://www.youtube.com/watch?v={vid_id}"
        duration = entry.get("duration")
        upd      = entry.get("upload_date", "unknown")

        # Check DB
        row = sess.execute(
            text("SELECT status FROM processed_source_videos WHERE video_url = :u"),
            {"u": vid_url},
        ).fetchone()

        if row:
            db_status = f"✗ IN DB [{row[0]}]"
        elif duration is not None and duration < 60:
            db_status = f"✗ SHORT ({duration}s)"
        else:
            db_status = "✓ AVAILABLE"

        dur_str = f"{duration}s" if duration is not None else "None"
        print(f"  {i+1:<3}  {vid_id:<14}  {dur_str:>8}  {upd:<12}  {db_status}")

    available = sum(
        1 for e in entries
        if e
        and e.get("id")
        and not sess.execute(
            text("SELECT 1 FROM processed_source_videos WHERE video_url = :u"),
            {"u": f"https://www.youtube.com/watch?v={e.get('id')}"},
        ).fetchone()
        and (e.get("duration") is None or e.get("duration", 999) >= 60)
    )
    print(f"\n  AVAILABLE (not processed, not short): {available} / {len(entries)}")


def flush_processed_table():
    """Wipe the processed_source_videos table so all videos can be retried."""
    count = sess.execute(text("SELECT COUNT(*) FROM processed_source_videos")).scalar()
    confirm = input(
        f"\n  ⚠  WARNING: This will DELETE all {count} rows from processed_source_videos.\n"
        f"     All previously processed URLs will be treated as unprocessed.\n"
        f"     Type YES to confirm: "
    )
    if confirm.strip().upper() == "YES":
        sess.execute(text("DELETE FROM processed_source_videos"))
        sess.commit()
        print(f"  ✓ Deleted {count} rows. Table is now empty.")
    else:
        print("  Cancelled.")


def main():
    parser = argparse.ArgumentParser(description="Pool Channel Diagnostic Tool")
    parser.add_argument("--sector-id", type=int, default=None,
                        help="Only diagnose this sector")
    parser.add_argument("--flush", action="store_true",
                        help="Wipe processed_source_videos table (fresh start)")
    args = parser.parse_args()

    print(f"\n{'═'*70}")
    print(f"  UTube Pool Diagnostic  —  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═'*70}")

    if args.flush:
        flush_processed_table()
        return

    check_processed_table()

    sectors_q = sess.query(Sectors)
    if args.sector_id:
        sectors_q = sectors_q.filter(Sectors.id == args.sector_id)
    sectors = sectors_q.all()

    if not sectors:
        print("  No sectors found.")
        return

    for sector in sectors:
        print(f"\n\n{'═'*70}")
        print(f"  SECTOR #{sector.id}: {sector.name}")
        print(f"{'═'*70}")

        pool_channels = sector.channels
        owned_ch      = sector.owned_channels

        print(f"  Pool channels (source) : {len(pool_channels)}")
        print(f"  Owned channels (output): {len(owned_ch)}")

        if not pool_channels:
            print("  ✗ NO POOL CHANNELS — add channels to this sector first!")
            continue

        for ch in pool_channels:
            diagnose_channel(ch)

    print(f"\n\n{'═'*70}")
    print("  Diagnostic complete.")
    print(f"{'═'*70}\n")


if __name__ == "__main__":
    main()
