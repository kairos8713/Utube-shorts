import datetime as dt
import glob
import os

import pytest

from crypto_utils import decrypt, is_encrypted
from fakes import FakeHost, FakeIG, ig_error
from helpers import NOW, account, post, source, video
from instagram import errors as E
from instagram import oauth, token_refresh
from instagram.client import IGClient
from instagram.publisher import publish_item
from models import Alert, OwnedChannels, Post, UploadQueue, videos


def _no_network(*a, **k):
    raise AssertionError("network used in DRY_RUN")


# ── client ───────────────────────────────────────────────────────────────────

def test_dry_run_sends_nothing_and_logs_request():
    c = IGClient("tok", "123", dry_run=True, transport=_no_network)
    cont = c.create_reel_container("cap", video_url="https://x/v.mp4", thumb_offset_ms=4200)
    assert c.container_status(cont["id"])["status_code"] == "FINISHED"
    assert c.publish(cont["id"])["id"].startswith("dry_media_")
    req = c.dry_log[0]
    assert req["url"] == "https://graph.instagram.com/v25.0/123/media"
    assert req["params"]["media_type"] == "REELS" and req["params"]["thumb_offset"] == 4200
    assert req["params"]["share_to_feed"] == "true"
    assert "tok" not in str(c.dry_log)


def test_client_has_no_destructive_methods():
    names = [n.lower() for n in dir(IGClient)]
    for bad in ("delete", "revoke", "password", "disconnect", "remove"):
        assert not [n for n in names if bad in n], bad


@pytest.mark.parametrize("status,body,kind", [
    (400, {"error": {"code": 190, "message": "Error validating access token"}}, E.AUTH),
    (400, {"error": {"code": 10, "message": "no permission"}}, E.PERMISSION),
    (400, {"error": {"code": 4, "message": "Application request limit reached"}}, E.RATE_LIMIT),
    (400, {"error": {"code": 9, "error_subcode": 2207042, "message": "publishing limit"}}, E.PUBLISH_LIMIT),
    (400, {"error": {"code": 368, "message": "blocked from posting, looks like spam"}}, E.ACTION_BLOCKED),
    (500, {"error": {"code": 2, "message": "Service temporarily unavailable"}}, E.TRANSIENT),
    (400, {"error": {"code": 9007, "message": "Media ID is not available"}}, E.NOT_READY),
])
def test_error_classification(status, body, kind):
    assert E.classify(status, body).kind == kind


def test_deprecation_header_reported_and_transient_retried():
    seen, calls = [], []

    def transport(method, url, params=None, headers=None, body=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            return 503, {"error": {"code": 2, "message": "temporarily unavailable"}}, {}
        return 200, {"id": "me"}, {"Sunset": "2027-01-21", "X-Business-Use-Case-Usage": "{}"}
    c = IGClient("t", "1", dry_run=False, transport=transport, on_deprecation=seen.append, sleep=lambda s: None)
    assert c.me()["id"] == "me" and len(calls) == 2
    assert seen and "Sunset" in seen[0]


# ── OAuth ────────────────────────────────────────────────────────────────────

def test_authorize_url_and_complete_login(db):
    url = oauth.authorize_url("st8")
    for s in oauth.SCOPES:
        assert s in url
    assert "response_type=code" in url and "state=st8" in url

    def http(method, u, params=None, data=None):
        if u == oauth.CODE_EXCHANGE_URL:
            assert data["code"] == "abc" and data["grant_type"] == "authorization_code"
            return {"data": [{"access_token": "short", "user_id": 1784}]}
        assert params["grant_type"] == "ig_exchange_token"
        return {"access_token": "LONG", "token_type": "bearer", "expires_in": 5184000}
    a = account(db, "new", status="DISCONNECTED", token=False)
    oauth.complete_login(db, a, "abc#_", http=http, me_fn=lambda t: {"user_id": "1784", "username": "neo"})
    assert is_encrypted(a.ig_access_token) and decrypt(a.ig_access_token) == "LONG"
    assert a.ig_username == "neo" and a.status == "WARMUP"
    assert 59 <= (a.ig_token_expires_at - a.ig_token_refreshed_at).days <= 60


# ── token refresh ────────────────────────────────────────────────────────────

def test_token_refresh_due_success_failure_and_warning(db):
    ok = account(db, "ok")
    ok.ig_token_expires_at, ok.ig_token_refreshed_at = NOW + dt.timedelta(days=8), NOW - dt.timedelta(days=52)
    bad = account(db, "bad")
    bad.ig_token_expires_at, bad.ig_token_refreshed_at = NOW + dt.timedelta(days=5), NOW - dt.timedelta(days=55)
    fresh = account(db, "fresh")
    fresh.ig_token_expires_at, fresh.ig_token_refreshed_at = NOW + dt.timedelta(days=50), NOW
    db.commit()

    def refresh(tok):
        if tok == "tok-bad":
            raise RuntimeError("OAuthException")
        return {"access_token": "NEW", "expires_in": 5184000}
    out = token_refresh.run(db, NOW, refresh_fn=refresh, dry_run=False)
    assert out["refreshed"] == [ok.id] and bad.id in out["failed"] and bad.id in out["warned"]
    assert decrypt(db.get(OwnedChannels, ok.id).ig_access_token) == "NEW"
    kinds = {a.kind for a in db.query(Alert).all()}
    assert {"TOKEN_REFRESH_FAILED", "TOKEN_EXPIRING"} <= kinds
    assert glob.glob(os.path.join(os.environ["LOG_DIR"], "outbox", "*TOKEN_*.eml"))


def test_token_refresh_dry_run_sends_nothing(db):
    a = account(db, "d")
    a.ig_token_expires_at, a.ig_token_refreshed_at = NOW + dt.timedelta(days=3), NOW - dt.timedelta(days=57)
    db.commit()
    out = token_refresh.run(db, NOW, refresh_fn=_no_network, dry_run=True)
    assert out["refreshed"] == [] and a.id in out["warned"]


# ── publisher ────────────────────────────────────────────────────────────────

@pytest.fixture
def queued(db, tmp_path):
    a = account(db, "pub")
    f = tmp_path / "reel.mp4"
    f.write_bytes(b"0" * 1000)
    v = video(db, a, path=str(f))
    v.cover_offset_ms = 3000
    q = UploadQueue(owned_channel_id=a.id, video_id=v.id, scheduled_for=NOW, status="PENDING", slot_source="qwen")
    db.add(q)
    db.commit()
    return a, v, q


def _pub(db, q, ig, host=None):
    host = host or FakeHost()
    return publish_item(db, q, now=NOW, client_factory=lambda acc, **k: ig, host=host, sleep=lambda s: None), host


def test_publish_happy_path(db, queued):
    a, v, q = queued
    ig = FakeIG()
    res, host = _pub(db, q, ig)
    assert res == "PUBLISHED"
    p = db.query(Post).one()
    assert p.status == "PUBLISHED" and p.ig_media_id == "M1" and p.slot_source == "qwen"
    assert q.status == "UPLOADED" and db.get(videos, v.id).is_used
    assert ("container", "https://pub.example/k1.mp4", 3000) in ig.calls
    assert host.deleted == ["k1"]                         # temp object removed


def test_double_publish_guard_finds_existing_media(db, queued):
    a, v, q = queued
    ig = FakeIG(publish_errors=[ig_error(E.TRANSIENT)], recent=[{"id": "M9", "caption": v.caption}])
    res, _ = _pub(db, q, ig)
    assert res == "PUBLISHED" and ig.calls.count("publish") == 1
    assert db.query(Post).one().ig_media_id == "M9"


def test_double_publish_guard_never_publishes_twice(db, queued):
    a, v, q = queued
    ig = FakeIG(publish_errors=[ig_error(E.TRANSIENT)], recent=[])
    res, _ = _pub(db, q, ig)
    assert res == "UNCERTAIN" and ig.calls.count("publish") == 1


def test_auth_error_pauses_account_and_holds_queue(db, queued):
    a, v, q = queued
    other = UploadQueue(owned_channel_id=a.id, video_id=video(db, a, url="u2").id,
                        scheduled_for=NOW + dt.timedelta(days=1), status="PENDING")
    db.add(other)
    db.commit()
    res, _ = _pub(db, q, FakeIG(container_error=ig_error(E.AUTH, "token invalid")))
    assert res == "PAUSED"
    a = db.get(OwnedChannels, a.id)
    assert a.status == "PAUSED_AUTO" and "AUTH" in a.pause_reason
    assert db.get(UploadQueue, other.id).status == "HELD"
    assert db.query(Alert).filter_by(kind="ACCOUNT_AUTO_PAUSED").count() == 1


def test_three_consecutive_failures_pause(db, queued):
    a, v, q = queued
    a.consecutive_failures = 2
    db.commit()
    res, _ = _pub(db, q, FakeIG(container_error=ig_error(E.UNKNOWN, "weird")))
    assert res == "PAUSED" and db.get(OwnedChannels, a.id).status == "PAUSED_AUTO"


def test_publish_limit_defers_to_next_day(db, queued):
    a, v, q = queued
    res, _ = _pub(db, q, FakeIG(quota=50))
    assert res == "DEFER_LIMIT" and q.status == "PENDING" and q.scheduled_for > NOW + dt.timedelta(hours=12)
    assert db.get(OwnedChannels, a.id).status == "ACTIVE"


def test_copyright_match_aborts(db, queued):
    a, v, q = queued
    res, _ = _pub(db, q, FakeIG(copyright_match=True))
    assert res == "COPYRIGHT" and q.status == "FAILED" and "publish" not in FakeIG().calls


def test_emergency_stop_prevents_any_call(db, queued):
    import system_state
    a, v, q = queued
    system_state.set_emergency_stop(True, session=db)

    def factory(acc, **k):
        raise AssertionError("client must not be created")
    assert publish_item(db, q, now=NOW, client_factory=factory) == "STOPPED"
    system_state.set_emergency_stop(False, session=db)
