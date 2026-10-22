"""1a: non-Instagram exceptions, R2 configuration errors, interrupted publishes, stuck items."""
import datetime as dt
import glob
import os

import pytest

from fakes import FakeHost, FakeIG
from helpers import NOW, account, video
from instagram import hosting
from instagram.publisher import RECONCILE_MIN_AGE, publish_item, reconcile_uncertain
from models import Alert, OwnedChannels, Post, UploadQueue


@pytest.fixture
def two_queued(db, tmp_path):
    a = account(db, "rob")
    b = account(db, "rob2")
    items = []
    for acc in (a, b):
        f = tmp_path / f"{acc.name}.mp4"
        f.write_bytes(b"0" * 1000)
        v = video(db, acc, url=f"u-{acc.name}", path=str(f))
        q = UploadQueue(owned_channel_id=acc.id, video_id=v.id, scheduled_for=NOW, status="PENDING")
        db.add(q)
        items.append(q)
    db.commit()
    return a, b, items


class BoomHost(FakeHost):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def upload_temp(self, path, dry_run=None):
        raise self.exc


def _pub(db, q, ig=None, host=None):
    return publish_item(db, q, now=NOW, client_factory=lambda acc, **k: ig or FakeIG(), host=host or FakeHost(),
                        sleep=lambda s: None)


def test_non_ig_exception_before_container_is_a_local_retry(db, two_queued):
    a, _, (q, _) = two_queued
    a.consecutive_failures = 2
    db.commit()
    assert _pub(db, q, host=BoomHost(OSError("disk read error"))) == "RETRY_LOCAL"
    p = db.query(Post).one()
    assert p.status == "FAILED" and p.error_kind == "EXCEPTION" and "OSError" in p.error_message
    assert q.status == "PENDING" and q.retry_count == 1 and q.scheduled_for == NOW + dt.timedelta(minutes=30)
    acc = db.get(OwnedChannels, a.id)
    assert acc.consecutive_failures == 2 and acc.status == "ACTIVE"     # not counted, no auto-pause


@pytest.mark.parametrize("exc", [
    hosting.HostingNotConfigured("R2_ENDPOINT_URL missing"),
    type("NoCredentialsError", (Exception,), {})("Unable to locate credentials"),
])
def test_r2_not_configured_single_alert_no_retry_count(db, two_queued, exc):
    a, b, (q1, q2) = two_queued
    from publish_runner import publish_due
    out = publish_due(db, NOW, client_factory=lambda acc, **k: FakeIG(), host=BoomHost(exc), sleep=lambda s: None)
    assert [o for _, o in out] == ["R2_NOT_CONFIGURED"]          # stopped: the second item was not tried
    tried = q1 if q1.post_id else q2
    assert tried.status == "PENDING" and (tried.retry_count or 0) == 0
    assert db.query(Post).count() == 1
    al = db.query(Alert).filter_by(kind="R2_NOT_CONFIGURED").one()
    assert al.severity == "CRITICAL"
    assert glob.glob(os.path.join(os.environ["LOG_DIR"], "outbox", "*R2_NOT_CONFIGURED*.eml"))
    for acc in (a, b):
        assert (db.get(OwnedChannels, acc.id).consecutive_failures or 0) == 0


def test_r2_rejected_credentials_detected():
    from instagram.publisher import is_r2_config_error

    class ClientError(Exception):
        def __init__(self, code):
            self.response = {"Error": {"Code": code}}
    assert is_r2_config_error(ClientError("InvalidAccessKeyId"))
    assert is_r2_config_error(ClientError("SignatureDoesNotMatch"))
    assert not is_r2_config_error(ClientError("SlowDown"))
    assert not is_r2_config_error(ValueError("x"))


def test_exception_after_container_is_left_to_reconcile(db, two_queued):
    a, _, (q, _) = two_queued

    class PollBoom(FakeIG):
        def container_status(self, cid):
            raise ValueError("unexpected payload")
    assert _pub(db, q, ig=PollBoom()) == "INTERRUPTED"
    p = db.query(Post).one()
    assert p.status == "CONTAINER" and q.status == "PUBLISHING"
    assert (db.get(OwnedChannels, a.id).consecutive_failures or 0) == 0


def _stranded_pending_post(db, q, age):
    p = Post(owned_channel_id=q.owned_channel_id, video_id=q.video_id, queue_id=q.id, status="PENDING",
             created_at=NOW - age, caption="c")
    db.add(p)
    db.flush()
    q.post_id, q.status = p.id, "PUBLISHING"
    db.commit()
    return p


def test_reconcile_requeues_post_interrupted_before_container(db, two_queued):
    a, _, (q, _) = two_queued
    p = _stranded_pending_post(db, q, RECONCILE_MIN_AGE + dt.timedelta(minutes=5))
    out = reconcile_uncertain(db, NOW, lambda acc, **k: FakeIG())
    assert out["interrupted"] == 1
    assert db.get(Post, p.id).status == "FAILED" and db.get(Post, p.id).error_kind == "INTERRUPTED_BEFORE_CONTAINER"
    assert q.status == "PENDING" and q.scheduled_for == NOW and (q.retry_count or 0) == 0
    assert (db.get(OwnedChannels, a.id).consecutive_failures or 0) == 0


def test_reconcile_leaves_fresh_pending_post_alone(db, two_queued):
    _, _, (q, _) = two_queued
    p = _stranded_pending_post(db, q, dt.timedelta(minutes=3))
    assert reconcile_uncertain(db, NOW, lambda acc, **k: FakeIG())["interrupted"] == 0
    assert db.get(Post, p.id).status == "PENDING" and q.status == "PUBLISHING"


@pytest.mark.parametrize("age,alerted", [(dt.timedelta(hours=3), True), (dt.timedelta(minutes=50), False)])
def test_startup_alerts_items_stuck_in_publishing(db, two_queued, age, alerted):
    from publish_runner import alert_stuck_publishing
    _, _, (q, _) = two_queued
    p = Post(owned_channel_id=q.owned_channel_id, video_id=q.video_id, queue_id=q.id, status="UNKNOWN",
             created_at=NOW - age, caption="c")
    db.add(p)
    db.flush()
    q.post_id, q.status = p.id, "PUBLISHING"
    db.commit()
    assert bool(alert_stuck_publishing(db, NOW)) is alerted
    assert (db.query(Alert).filter_by(kind="PUBLISH_STUCK", severity="CRITICAL").count() == 1) is alerted
