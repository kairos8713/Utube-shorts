"""Decision #6: small fixes from the full test (pause reason, copyright e-mail, Ollama restart trace,
stop script, credit line)."""
import glob
import logging
import os

from fakes import FakeHost, FakeIG, ig_error
from helpers import NOW, account, video
from instagram import errors as E
from instagram.publisher import publish_item
from models import Alert, OwnedChannels, UploadQueue

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _queued(db, tmp_path, acc):
    f = tmp_path / f"{acc.name}.mp4"
    f.write_bytes(b"0" * 1000)
    q = UploadQueue(owned_channel_id=acc.id, video_id=video(db, acc, url=acc.name, path=str(f)).id,
                    scheduled_for=NOW, status="PENDING")
    db.add(q)
    db.commit()
    return q


def test_pause_reason_has_the_error_kind_once(db, tmp_path):
    a = account(db, "authfail")
    q = _queued(db, tmp_path, a)
    ig = FakeIG(container_error=ig_error(E.AUTH, "Error validating access token"))
    assert publish_item(db, q, now=NOW, client_factory=lambda acc, **k: ig, host=FakeHost(),
                        sleep=lambda s: None) == "PAUSED"
    reason = db.get(OwnedChannels, a.id).pause_reason
    assert reason.startswith("AUTH: ") and not reason.startswith("AUTH: AUTH:")


def test_copyright_match_is_emailed(db, tmp_path, monkeypatch):
    import email_notifier
    from config import settings
    from email_notifier import EMAIL_KINDS
    # own de-dup state: other tests may already have e-mailed a COPYRIGHT_MATCH within the 6 h window
    monkeypatch.setattr(email_notifier, "_state_path", lambda: str(tmp_path / "email_dedup.json"))
    assert "COPYRIGHT_MATCH" in EMAIL_KINDS
    a = account(db, "cr")
    q = _queued(db, tmp_path, a)
    before = set(glob.glob(os.path.join(settings.LOG_DIR, "outbox", "*COPYRIGHT_MATCH*.eml")))
    assert publish_item(db, q, now=NOW, client_factory=lambda acc, **k: FakeIG(copyright_match=True),
                        host=FakeHost(), sleep=lambda s: None) == "COPYRIGHT"
    alert = db.query(Alert).filter_by(kind="COPYRIGHT_MATCH").one()
    assert alert.emailed
    assert set(glob.glob(os.path.join(settings.LOG_DIR, "outbox", "*COPYRIGHT_MATCH*.eml"))) - before


def test_watchdog_logs_and_alerts_an_ollama_restart(db, monkeypatch):
    import watchdog
    from config import settings
    monkeypatch.setenv("LLM_BACKEND", "ollama")
    state = {"up": False}
    assert watchdog.check_ollama(db, NOW, health=lambda: state["up"],
                                 starter=lambda: state.update(up=True)) == "restarted"
    for h in logging.getLogger("watchdog").handlers:
        h.flush()
    text = "".join(open(f, encoding="utf-8", errors="replace").read()
                   for f in glob.glob(os.path.join(settings.LOG_DIR, "watchdog.log*")))
    assert "Ollama was not responding — restarted" in text
    alert = db.query(Alert).filter_by(kind="OLLAMA_RESTARTED").one()
    assert alert.severity == "WARNING"


def test_credit_line_ends_with_on_youtube_and_keeps_the_handle():
    import qwen_specialized as q
    cap = q.compose_caption("A sharp line.", ["#a1", "#b2", "#c3"], "JayShettyPodcast.")
    assert cap.splitlines()[-1] == "🎥 @JayShettyPodcast. on YouTube"
    assert q.compose_caption("x", ["#a1", "#b2", "#c3"], "", "Some Show").splitlines()[-1] == "🎥 Some Show on YouTube"


def test_stop_script_stops_tasks_without_removing_and_kills_child_trees():
    src = open(os.path.join(ROOT, "scripts", "stop_services.ps1"), encoding="utf-8").read()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "Stop-ScheduledTask" in code and "Unregister-ScheduledTask" not in code
    assert "ParentProcessId" in code and "Stop-Tree" in code              # whole child tree
    for script in ("watchdog.py", "panel.py", "publish_runner.py", "comment_responder.py", "cycle_runner.py"):
        assert script in code
    assert "ollama" not in code.lower()                                   # never touches Ollama
