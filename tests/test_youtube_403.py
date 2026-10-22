"""Decision #4: a 403 on one video is retried once (other format) and then only that video is skipped;
YOUTUBE_BLOCKED only after 403/429 on 3 different videos within 1 hour. Bot-check stays an instant block."""
import os

import pytest

import downloader
from models import Alert

FORBIDDEN = ("WARNING: android_vr client https formats require a GVS PO Token\n"
             "ERROR: unable to download video data: HTTP Error 403: Forbidden")


class FakeRun:
    """subprocess.run stand-in: video ids in `bad` always 403, the rest succeed (file written)."""

    def __init__(self, bad=(), fail_first_only=()):
        self.bad, self.fail_first_only, self.calls = set(bad), set(fail_first_only), []

    def __call__(self, cmd, **kw):
        url = cmd[-1]
        vid = downloader.video_id_from_url(url)
        self.calls.append((vid, cmd[cmd.index("-f") + 1]))
        n = sum(1 for v, _ in self.calls if v == vid)
        if vid in self.bad or (vid in self.fail_first_only and n == 1):
            return type("R", (), {"returncode": 1, "stderr": FORBIDDEN, "stdout": ""})()
        out = cmd[cmd.index("-o") + 1]
        with open(out, "wb") as f:
            f.write(b"0" * 2_000_000)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()


@pytest.fixture
def yt(db, monkeypatch, tmp_path):
    import system_state
    system_state.put(downloader._STRIKES_KEY, "[]")
    waits = []
    monkeypatch.setattr(downloader, "_sleep", waits.append)
    monkeypatch.setattr(downloader, "_wait_stable", lambda p: None)
    monkeypatch.setattr(downloader, "_base_args", lambda: ["yt-dlp"])
    monkeypatch.setattr(downloader, "polite_delay", lambda *a, **k: None)
    return waits, tmp_path


def test_classification_bot_check_is_still_an_instant_block():
    assert isinstance(downloader.classify_ytdlp_error("ERROR: Sign in to confirm you're not a bot"),
                      downloader.YouTubeBlocked)
    e = downloader.classify_ytdlp_error(FORBIDDEN)
    assert isinstance(e, downloader.VideoForbidden) and not isinstance(e, downloader.YouTubeBlocked)
    assert downloader.classify_ytdlp_error("ERROR: HTTP Error 429: Too Many Requests").kind == "RATE_LIMITED_429"


def test_single_403_waits_and_retries_once_with_avc1_format(yt, monkeypatch):
    waits, tmp = yt
    run = FakeRun(fail_first_only={"vidAAAAAAA1"})
    monkeypatch.setattr(downloader.subprocess, "run", run)
    path = downloader.download_youtube_video("https://www.youtube.com/watch?v=vidAAAAAAA1", out_dir=str(tmp))
    assert os.path.exists(path)
    assert [f for _, f in run.calls][1] == downloader.FORBIDDEN_RETRY_FORMAT
    assert len(waits) == 1 and 60 <= waits[0] <= 120


def test_one_video_403_skips_only_that_video_and_the_cycle_goes_on(yt, monkeypatch, tmp_path):
    from production import SourceVideo
    from test_production_pipeline import make_pipeline
    waits, tmp = yt
    run = FakeRun(bad={"badVIDEO001"})
    monkeypatch.setattr(downloader.subprocess, "run", run)
    p = make_pipeline(tmp_path, download_fn=lambda url: downloader.download_youtube_video(url, out_dir=str(tmp)))
    res = p.run([SourceVideo(url="https://www.youtube.com/watch?v=badVIDEO001", niche="x", source_handle="S"),
                 SourceVideo(url="https://www.youtube.com/watch?v=goodVIDEO01", niche="x", source_handle="S")])
    assert res.blocked is None and len(res.produced) == 1
    assert "FORBIDDEN_403" in res.source_errors[0].error
    assert [v for v, _ in run.calls] == ["badVIDEO001", "badVIDEO001", "goodVIDEO01"]   # 1 retry, then next video


def test_403_on_three_different_videos_blocks_with_one_alert(yt, db, monkeypatch):
    import cycle_runner
    import sources_manager
    from helpers import NOW, account
    from llm_mock import MockLLM
    from production import SourceVideo
    waits, tmp = yt
    run = FakeRun(bad={"bad0000000A", "bad0000000B", "bad0000000C", "bad0000000D"})
    monkeypatch.setattr(downloader.subprocess, "run", run)
    monkeypatch.setattr(downloader, "update_ytdlp_once", lambda: None)
    monkeypatch.setattr(downloader, "cookies_age_days", lambda: 1.0)
    monkeypatch.setattr(sources_manager, "cycle_maintenance", lambda s, now, llm=None: {})
    monkeypatch.setattr(sources_manager, "plan_production", lambda s, needs, now: [
        SourceVideo(url=f"https://www.youtube.com/watch?v=bad0000000{c}", niche="x") for c in "ABCD"])
    account(db, "needs_clips")
    stats = cycle_runner.run_one_cycle(db, NOW, llm=MockLLM())
    assert stats["youtube_blocked"] == "FORBIDDEN_403_REPEATED"
    assert [v for v, _ in run.calls] == ["bad0000000A"] * 2 + ["bad0000000B"] * 2 + ["bad0000000C"] * 2  # D never tried
    alerts = db.query(Alert).filter_by(kind="YOUTUBE_BLOCKED").all()
    assert len(alerts) == 1 and "3+ different videos" in alerts[0].message


def test_strikes_older_than_an_hour_do_not_count(yt):
    assert downloader.record_forbidden("v1", now=1000.0) == 1
    assert downloader.record_forbidden("v2", now=1000.0 + 1800) == 2
    assert downloader.record_forbidden("v3", now=1000.0 + 3700) == 2        # v1 aged out
    assert downloader.record_forbidden("v3", now=1000.0 + 3800) == 2        # same video counts once
