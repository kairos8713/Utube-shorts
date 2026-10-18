import datetime as dt
import json

import pytest
from werkzeug.security import generate_password_hash

import system_state
from helpers import NOW, account, post, source, video
from models import Alert, Comment, CommentReply, CycleReport, Decision, OwnedChannels, PostMetric, UploadQueue
from test_security import CsrfClient

PAGES = ["/", "/accounts", "/today", "/sources", "/sources?status=ACTIVE", "/reports", "/decisions",
         "/decisions?task=slot_plan&applied=0", "/comments", "/guardrails"]


@pytest.fixture
def panel(monkeypatch, db):
    from web import auth, create_app
    auth.reset_rate_limit()
    monkeypatch.setenv("PANEL_PASSWORD_HASH", generate_password_hash("s3cret-pass!"))
    c = CsrfClient(create_app(testing=True).test_client())
    assert c.post("/login", data={"username": "emre", "password": "s3cret-pass!"}).status_code == 302
    return c


def _seed(db):
    a = account(db, "seed")
    paused = account(db, "paused", status="PAUSED_AUTO")
    paused.pause_reason = "AUTH: token invalid"
    src = source(db)
    v = video(db, a, source_id=src.id)
    p = post(db, a, v)
    p.permalink = "https://instagram.com/reel/x"
    db.add(PostMetric(post_id=p.id, checkpoint_h=24, views=100))
    db.add(UploadQueue(owned_channel_id=a.id, video_id=video(db, a, url="q").id, status="PENDING",
                       scheduled_for=dt.datetime.utcnow() + dt.timedelta(hours=1), slot_source="explore"))
    db.add(Alert(kind="ACCOUNT_AUTO_PAUSED", severity="CRITICAL", message="paused!", owned_channel_id=paused.id))
    c = Comment(ig_comment_id="x1", post_id=p.id, owned_channel_id=a.id, username="u", text="my video!",
                needs_attention=True, status="RISKY", commented_at=NOW)
    db.add(c)
    db.flush()
    db.add(CommentReply(comment_id=c.id, owned_channel_id=a.id, text="thanks", scheduled_for=NOW, status="SENT"))
    db.add(Decision(task="slot_plan", proposed_action="{}", validation="REJECTED: x", applied=False))
    db.add(CycleReport(cycle_id=1, started_at=NOW - dt.timedelta(days=3), finished_at=NOW,
                       stats_json=json.dumps(__import__("reports").collect_stats(db, NOW - dt.timedelta(days=3), NOW)),
                       narrative="özet", decision_summary="ok"))
    db.commit()
    return a, paused


def test_all_pages_render(panel, db):
    _seed(db)
    for url in PAGES:
        r = panel.get(url)
        assert r.status_code == 200, (url, r.data[:300])
    assert "ACİL DURDURMA".encode() in panel.get("/").data
    assert b"AUTH: token invalid" in panel.get("/accounts").data


def test_pages_require_login(db):
    from web import create_app
    c = create_app(testing=True).test_client()
    for url in PAGES:
        assert c.get(url).status_code == 302


def test_post_without_csrf_rejected(panel):
    assert panel.c.post("/emergency", data={"action": "stop", "confirm": "DURDUR"}).status_code == 400


def test_emergency_stop_needs_typed_confirmation(panel, db):
    panel.post("/emergency", data={"action": "stop", "confirm": "yes"})
    assert not system_state.emergency_stopped()
    panel.post("/emergency", data={"action": "stop", "confirm": "DURDUR"})
    assert system_state.emergency_stopped()
    panel.post("/emergency", data={"action": "clear", "confirm": "DEVAM"})
    assert not system_state.emergency_stopped()


def test_resume_only_from_panel_releases_held_queue(panel, db):
    a, paused = _seed(db)
    db.add(UploadQueue(owned_channel_id=paused.id, video_id=video(db, paused, url="h").id, status="HELD",
                       scheduled_for=dt.datetime.utcnow() + dt.timedelta(hours=5)))
    db.commit()
    panel.post(f"/accounts/{paused.id}/resume")
    db.expire_all()
    assert db.get(OwnedChannels, paused.id).status == "ACTIVE"
    assert db.query(UploadQueue).filter_by(owned_channel_id=paused.id, status="HELD").count() == 0


def test_setup_date_and_new_account(panel, db):
    panel.post("/accounts/new", data={"name": "fresh_acc", "niche": "Tech / AI (Riskli)"})
    acc = db.query(OwnedChannels).filter_by(name="fresh_acc").one()
    assert acc.status == "DISCONNECTED" and acc.sector.music_slug == "tech_ai_riskli"
    panel.post(f"/accounts/{acc.id}/setup", data={"setup_completed_at": "2026-10-24T15:30"})
    db.expire_all()
    assert db.get(OwnedChannels, acc.id).ig_setup_completed_at == dt.datetime(2026, 10, 24, 13, 30)


def test_oauth_start_redirects_with_state(panel, db):
    a, _ = _seed(db)
    r = panel.get(f"/oauth/instagram/start/{a.id}")
    assert r.status_code == 302 and "instagram.com/oauth/authorize" in r.headers["Location"]
    bad = panel.post("/oauth/instagram/paste", data={"url": "https://x/cb?code=abc&state=forged"})
    assert bad.status_code == 302
    db.expire_all()
    assert db.get(OwnedChannels, a.id).ig_access_token.startswith("enc:v1:")     # unchanged (old token)
