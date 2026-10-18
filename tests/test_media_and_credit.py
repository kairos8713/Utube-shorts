import random

import pytest

import media_utils
from credit import handle_from_url, source_handle
from downloader import VideoUnavailable, YouTubeBlocked, classify_ytdlp_error


def test_music_slug_matches_folders():
    assert media_utils.music_slug("Tech / AI (Riskli)") == "tech_ai_riskli"
    assert media_utils.music_slug("Food & Cooking (Riskli)") == "food_cooking_riskli"


def test_pick_bg_music_fallbacks(tmp_path, monkeypatch):
    (tmp_path / "tech_ai_riskli").mkdir()
    (tmp_path / "tech_ai_riskli" / "a.mp3").write_bytes(b"x")
    (tmp_path / "default").mkdir()
    (tmp_path / "default" / "d.mp3").write_bytes(b"x")
    monkeypatch.setattr(media_utils, "BG_MUSIC_DIR", str(tmp_path))
    rng = random.Random(1)
    assert media_utils.pick_bg_music("Tech / AI (Riskli)", rng=rng).endswith("a.mp3")
    assert media_utils.pick_bg_music("Unknown niche", rng=rng).endswith("d.mp3")
    assert media_utils.pick_bg_music("x", slug="tech_ai_riskli", rng=rng).endswith("a.mp3")


def test_repo_bg_music_has_no_duplicate_folders():
    slugs = media_utils.available_music_slugs()
    assert "tech__ai_riskli" not in slugs and "food__cooking_riskli" not in slugs
    assert "automative_riskli" not in slugs


def test_intervals_overlap():
    assert media_utils.intervals_overlap(10, 30, 25, 50)
    assert not media_utils.intervals_overlap(10, 30, 30, 50)
    assert media_utils.intervals_overlap(10, 30, 31, 50, tolerance=2)


@pytest.mark.parametrize("url,handle", [
    ("https://www.youtube.com/@MrBeast", "MrBeast"),
    ("https://www.youtube.com/@Some.Handle_1/videos", "Some.Handle_1"),
    ("https://www.youtube.com/c/OldName", "OldName"),
    ("https://www.youtube.com/channel/UCabc", ""),
    ("", ""),
])
def test_handle_from_url(url, handle):
    assert handle_from_url(url) == handle


def test_source_handle_uses_source_not_owned_account(db):
    from models import Channels, OwnedChannels, Sectors
    sec = Sectors(name="Tech", title="t", description="d", tags="t")
    db.add(sec)
    db.flush()
    ch = Channels(url="https://www.youtube.com/@RealSource", sector_id=sec.id)
    acc = OwnedChannels(name="our_account", sector_id=sec.id, level=1, mail="m", password="p")
    db.add_all([ch, acc])
    db.commit()
    assert source_handle(db, legacy_channel_id=ch.id) == "RealSource"


def test_classify_ytdlp_errors():
    assert isinstance(classify_ytdlp_error("ERROR: Sign in to confirm you're not a bot"), YouTubeBlocked)
    e = classify_ytdlp_error("ERROR: The provided YouTube account cookies are no longer valid")
    assert isinstance(e, YouTubeBlocked) and e.kind == "COOKIES"
    assert isinstance(classify_ytdlp_error("ERROR: Private video"), VideoUnavailable)
