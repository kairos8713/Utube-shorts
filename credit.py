"""
credit.py — resolve the ORIGINAL source handle for on-screen credit + caption.

Bug fixed here: the old sector_processor used an undefined `pool_ch`, so the
credit was always empty, and passed *our own* account name to Qwen as the
"source channel". The handle now always comes from the source channel row
(sources table first, legacy channels table as fallback).
"""
import re
from typing import Optional


def handle_from_url(url: Optional[str]) -> str:
    """https://www.youtube.com/@Some.Handle/videos → 'Some.Handle'."""
    if not url:
        return ""
    url = url.strip()
    m = re.search(r"/@([A-Za-z0-9._-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/(?:c|user)/([A-Za-z0-9._-]+)", url)
    if m:
        return m.group(1)
    if url.startswith("@"):
        return re.sub(r"[^A-Za-z0-9._-]", "", url[1:])
    return ""


def source_handle(session, source_id: Optional[int] = None, legacy_channel_id: Optional[int] = None) -> str:
    """Return the credit handle for a clip's source. Never returns our own account name."""
    from models import Channels
    try:
        from models import Source
    except ImportError:          # before the Phase-4 schema exists
        Source = None

    if source_id and Source is not None:
        src = session.get(Source, source_id)
        if src is not None:
            return (src.handle or handle_from_url(src.channel_url) or "").lstrip("@")
    if legacy_channel_id:
        ch = session.get(Channels, legacy_channel_id)
        if ch is not None:
            return handle_from_url(ch.url)
    return ""
