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
