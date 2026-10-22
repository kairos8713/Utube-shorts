"""
review_sources.py — which sources in the database break the source rules (source_rules.py)?

READ-ONLY: prints a PROPOSED blacklist; nothing in the database is changed. Apply the proposals in the
panel (Kaynaklar → kara liste) after checking them.

    .venv\\Scripts\\python.exe scripts\\review_sources.py              # rules on stored data, no network
    .venv\\Scripts\\python.exe scripts\\review_sources.py --online     # + YouTube category / descriptions
    .venv\\Scripts\\python.exe scripts\\review_sources.py --db path\\to\\automation.db --json

Offline checks: exclusion list (channel id, title, handle), reaction/compilation share of the stored
recent titles, explicit clip-permission wording in the stored description. --online adds the YouTube
category of the newest long video (Music → exclude) and the last 15 video descriptions (metadata only,
no download, a short pause between channels).
"""
import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import source_rules  # noqa: E402


def _db_path(arg: str = None) -> str:
    if arg:
        return arg
    from config import settings
    url = settings.DATABASE_URL
    if not url.startswith("sqlite:///"):
        raise SystemExit(f"only sqlite databases are supported: {url}")
    return url[len("sqlite:///"):]


def open_read_only(path: str) -> sqlite3.Connection:
    """mode=ro while a WAL holds unmerged writes (services running), else immutable=1 (creates no
    -wal/-shm files and takes no locks)."""
    p = os.path.abspath(path).replace("\\", "/")
    wal = path + "-wal"
    flags = "mode=ro" if os.path.exists(wal) and os.path.getsize(wal) > 0 else "mode=ro&immutable=1"
    return sqlite3.connect(f"file:{p}?{flags}", uri=True)


def review(db_path: str, online: bool = False, yt=None) -> list:
    con = open_read_only(db_path)
    try:
        rows = con.execute("SELECT id, title, handle, yt_channel_id, channel_url, status, description, "
                           "recent_titles, clip_permission FROM sources ORDER BY id").fetchall()
    finally:
        con.close()
    out = []
    for sid, title, handle, cid, url, status, desc, titles_json, perm in rows:
        try:
            titles = json.loads(titles_json or "[]")
        except ValueError:
            titles = []
        videos = [(t, "") for t in titles]
        cats = None
        if online and status != "BLACKLISTED":
            videos, cats = _online(yt, url, videos)
        why = source_rules.code_exclusion(title or "", handle or "", cid or "", videos, cats)
        perm_now, evidence = source_rules.permission_from_text(desc or "")
        out.append({"id": sid, "title": title, "channel_url": url, "status": status,
                    "propose": "BLACKLIST" if why and status != "BLACKLISTED" else "",
                    "reason": why or "", "clip_permission_stored": perm,
                    "clip_permission_text": "unknown" if perm_now is None else perm_now,
                    "permission_evidence": evidence})
    return out


def _online(yt, url, videos):
    import downloader
    from sources_manager import YouTubeAPI, channel_stats
    yt = yt or YouTubeAPI()
    try:
        stats = channel_stats(yt.channel_videos(url, source_rules.RECENT_VIDEOS))
        cats = yt.video_categories(stats["sample_video"]) if stats.get("sample_video") else None
        yt.delay()
        return stats.get("recent_videos") or videos, cats
    except downloader.DownloadError as e:
        print(f"  ! {url}: {e}", file=sys.stderr)
        return videos, None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="sqlite file (default: DATABASE_URL from .env)")
    ap.add_argument("--online", action="store_true", help="also check YouTube category / descriptions")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = review(_db_path(a.db), online=a.online)
    if a.json:
        print(json.dumps(res, indent=1, ensure_ascii=False))
        return res
    proposals = [r for r in res if r["propose"]]
    print(f"{len(res)} sources checked (read-only) — {len(proposals)} proposed for the blacklist:\n")
    for r in proposals:
        print(f"  #{r['id']:<4} {r['status']:<10} {r['title'] or r['channel_url']}\n         → {r['reason']}")
    unknown = [r for r in res if r["clip_permission_text"] == "unknown" and r["status"] in ("TRIAL", "ACTIVE")]
    if unknown:
        print(f"\n{len(unknown)} TRIAL/ACTIVE sources say nothing about clipping (permission unknown):")
        for r in unknown:
            print(f"  #{r['id']:<4} {r['title'] or r['channel_url']}")
    print("\nNothing was changed. Apply in the panel: Kaynaklar → kara liste.")
    return res


if __name__ == "__main__":
    main()
