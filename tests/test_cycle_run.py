from helpers import NOW, account
from llm_mock import MockLLM
from models import Alert


class DownLLM(MockLLM):
    def health(self):
        return False


def test_cycle_skips_production_when_llm_down(db, monkeypatch):
    import cycle_runner
    import downloader
    import sources_manager
    monkeypatch.setattr(downloader, "update_ytdlp_once", lambda: None)
    monkeypatch.setattr(downloader, "cookies_age_days", lambda: 1.0)
    monkeypatch.setattr(sources_manager, "cycle_maintenance", lambda s, now, llm=None: {})
    account(db, "needs_clips")
    stats = cycle_runner.run_one_cycle(db, NOW, llm=DownLLM())
    assert stats["production_skipped"] == "llm unavailable"
    assert db.query(Alert).filter_by(kind="LLM_UNAVAILABLE").count() == 1


def test_youtube_blocked_alert_gives_the_kind_specific_fix(db, monkeypatch):
    """PO-token 403s are not a cookie problem (the working set-up runs without cookies)."""
    import cycle_runner
    import downloader
    import sources_manager
    from production import BatchResult
    monkeypatch.setattr(downloader, "update_ytdlp_once", lambda: None)
    monkeypatch.setattr(downloader, "cookies_age_days", lambda: 1.0)
    monkeypatch.setattr(sources_manager, "cycle_maintenance", lambda s, now, llm=None: {})
    monkeypatch.setattr(sources_manager, "plan_production", lambda s, needs, now: [])

    class BlockedPipeline:
        def __init__(self, **hooks):
            pass

        def run(self, plan):
            return BatchResult(blocked="FORBIDDEN_403_PO_TOKEN")
    account(db, "needs_clips")
    stats = cycle_runner.run_one_cycle(db, NOW, pipeline_factory=BlockedPipeline, llm=MockLLM())
    assert stats["youtube_blocked"] == "FORBIDDEN_403_PO_TOKEN"
    msg = db.query(Alert).filter_by(kind="YOUTUBE_BLOCKED").one().message
    assert "yt-dlp" in msg and "refresh cookies" not in msg
    assert "WITHOUT cookies" in downloader.blocked_hint("COOKIES")
