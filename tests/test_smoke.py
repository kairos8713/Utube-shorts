import datetime as dt
import importlib
import os
import re

import pytest

from helpers import NOW, account, post, video

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTIVE = ["alerts", "analytics", "brain", "comment_responder", "config", "cover_frame", "credit", "crypto_utils",
          "cycle_runner", "db_migrate", "dedup", "downloader", "email_notifier", "ffmpeg_renderer", "gpu_lock",
          "guardrails", "instagram.client", "instagram.oauth", "instagram.publisher", "instagram.token_refresh",
          "instagram.hosting", "jobs", "llm_client", "llm_mock", "log_setup", "maintenance", "media_utils", "models",
          "panel", "power", "production", "production_store", "prompt_safety", "publish_runner", "qwen_specialized",
          "render_qc", "reports", "rife_interpolator", "scheduler", "sources_manager", "synthetic_media",
          "system_state", "timeutil", "transcriber", "watchdog", "web.views"]
LEGACY = {"publisher", "legacy_runner", "sector_processor", "weekly_cycle", "fulfillment_engine", "update_stats",
          "youtube_analytics", "fetch_channel_stats", "multimodal_context", "daily_production", "publish_on_connect",
          "ig_dashboard", "esrgan_upscaler", "kokoro_onnx", "moviepy", "instagrapi", "transformers"}


@pytest.mark.parametrize("mod", ACTIVE)
def test_module_imports(mod):
    importlib.import_module(mod)


def test_active_code_never_imports_legacy():
    offenders = []
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("legacy_youtube", "legacy_misc", "Practical-RIFE", "tests",
                                                "backups", "logs", ".git", "__pycache__")]
        for f in files:
            if not f.endswith(".py"):
                continue
            text = open(os.path.join(dirpath, f), encoding="utf-8").read()
            for m in re.finditer(r"^\s*(?:from|import)\s+([\w.]+)", text, re.M):
                if m.group(1).split(".")[0] in LEGACY:
                    offenders.append((f, m.group(1)))
    assert offenders == []


def test_no_seven_day_cycle_constants_left():
    import cycle_runner
    assert cycle_runner.CYCLE_DAYS == 3
    for f in ("cycle_runner.py", "scheduler.py", "production_store.py"):
        text = open(os.path.join(ROOT, f), encoding="utf-8").read()
        assert "DAYS_IN_CYCLE" not in text and "NEXT_CYCLE_OFFSET_DAYS" not in text


def test_build_cycle_report_with_mock(db):
    import reports
    a = account(db, "rep")
    post(db, a, video(db, a))
    rep = reports.build_cycle_report(db, 1, NOW - dt.timedelta(days=3), NOW + dt.timedelta(hours=1))
    assert rep.narrative and '"summary"' in rep.stats_json
