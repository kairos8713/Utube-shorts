"""Tests for the post-review fixes (items 1–9)."""
import datetime as dt
import glob
import os
import random
import secrets
import subprocess

import pytest

import comment_responder as cr
import guardrails
from crypto_utils import encrypt
from fakes import FakeHost, FakeIG, ig_error
from helpers import NOW, account, post, source, video
from instagram import errors as E
from instagram.publisher import RECONCILE_GIVE_UP, publish_item, reconcile_uncertain
from llm_client import set_llm
from llm_mock import MockLLM
from models import Alert, Comment, OwnedChannels, Post, Source, UploadQueue, videos

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def queued(db, tmp_path):
    a = account(db, "pub")
    f = tmp_path / "reel.mp4"
    f.write_bytes(b"0" * 1000)
    v = video(db, a, path=str(f))
    q = UploadQueue(owned_channel_id=a.id, video_id=v.id, scheduled_for=NOW, status="PENDING", slot_source="qwen")
    db.add(q)
    db.commit()
    return a, v, q


def _pub(db, q, ig, now=NOW, **kw):
    return publish_item(db, q, now=now, client_factory=lambda acc, **k: ig, host=FakeHost(),
                        sleep=lambda s: None, **kw)


def _outbox(kind):
    return glob.glob(os.path.join(os.environ["LOG_DIR"], "outbox", f"*{kind}*.eml"))


# ═════════════════════════════════════════════════════════════════════════════
# 1. double publish
# ═════════════════════════════════════════════════════════════════════════════

def test_a_network_error_on_publish_never_triggers_second_publish(db, queued):
    a, v, q = queued
    ig = FakeIG(publish_errors=[ig_error(E.TRANSIENT, "network: read timed out")], recent=[],
                status_seq=["FINISHED"])
    assert _pub(db, q, ig) == "UNCERTAIN"
    assert ig.calls.count("publish") == 1
    p = db.query(Post).one()
    assert p.status == "UNKNOWN" and p.error_kind == "UNCERTAIN"
    assert q.status == "HELD" and (q.retry_count or 0) == 0
    assert (db.get(OwnedChannels, a.id).consecutive_failures or 0) == 0
    # the runner never picks it up again by itself
    from publish_runner import publish_due
    assert publish_due(db, NOW + dt.timedelta(hours=1), client_factory=lambda acc, **k: ig,
                       host=FakeHost(), sleep=lambda s: None) == []
    assert ig.calls.count("publish") == 1


def test_unknown_error_and_published_container_are_uncertain_too(db, queued):
    a, v, q = queued
    ig = FakeIG(status_seq=["PUBLISHED"], recent=[])           # container says PUBLISHED, media not listed
    assert _pub(db, q, ig) == "UNCERTAIN" and "publish" not in ig.calls
    assert db.query(Post).one().status == "UNKNOWN"


def test_b_publish_succeeded_but_listed_late_reconcile_marks_published(db, queued):
    a, v, q = queued
    ig = FakeIG(publish_errors=[ig_error(E.TRANSIENT)], recent=[], status_seq=["FINISHED"])
    assert _pub(db, q, ig) == "UNCERTAIN"
    # 5 minutes later: too early, nothing happens
    assert reconcile_uncertain(db, NOW + dt.timedelta(minutes=5), lambda acc, **k: ig)["published"] == 0
    ig.recent = [{"id": "M_LATE", "caption": v.caption}]
    ig.status_seq = ["PUBLISHED"]
    out = reconcile_uncertain(db, NOW + dt.timedelta(minutes=20), lambda acc, **k: ig)
    assert out["published"] == 1
    p = db.query(Post).one()
    assert p.status == "PUBLISHED" and p.ig_media_id == "M_LATE" and p.error_kind is None
    assert q.status == "UPLOADED" and db.get(videos, v.id).is_used
    assert ig.calls.count("publish") == 1


def _crashed_post(db, a, v, q, status="CONTAINER", age=dt.timedelta(minutes=30)):
    p = Post(owned_channel_id=a.id, video_id=v.id, queue_id=q.id, status=status, caption=v.caption,
             ig_container_id="C9", container_created_at=NOW - age, created_at=NOW - age,
             source_url=v.source_url, segment_start=v.segment_start, segment_end=v.segment_end)
    db.add(p)
    q.status, q.post_id = "PUBLISHING", None
    db.flush()
    q.post_id = p.id
    db.commit()
    return p


def test_c_crash_in_container_stage_resolved_at_startup(db, queued):
    from publish_runner import startup_reconcile
    a, v, q = queued
    p = _crashed_post(db, a, v, q)
    ig = FakeIG(status_seq=["PUBLISHED"], recent=[{"id": "M_CRASH", "caption": v.caption}])
    out = startup_reconcile(db, NOW, client_factory=lambda acc, **k: ig)
    assert out["published"] == 1 and db.get(Post, p.id).status == "PUBLISHED" and q.status == "UPLOADED"
    assert "publish" not in ig.calls


def test_crash_not_published_after_2h_requeues_normally(db, queued):
    a, v, q = queued
    p = _crashed_post(db, a, v, q, age=dt.timedelta(minutes=40))
    ig = FakeIG(status_seq=["FINISHED"], recent=[])
    out = reconcile_uncertain(db, NOW, lambda acc, **k: ig)          # < 2 h: wait, item parked as HELD
    assert out["waiting"] == 1 and q.status == "HELD"
    later = NOW + RECONCILE_GIVE_UP
    out = reconcile_uncertain(db, later, lambda acc, **k: ig)
    assert out["failed"] == 1
    assert db.get(Post, p.id).status == "FAILED" and q.status == "PENDING" and q.scheduled_for == later
    assert not guardrails.cross_account_duplicate(db, a.id, v)     # the video may be published again


def test_still_unclear_after_2h_alerts_and_stays_held(db, queued):
    a, v, q = queued
    p = _crashed_post(db, a, v, q, status="UNKNOWN", age=dt.timedelta(hours=3))
    ig = FakeIG(status_seq=["PUBLISHED"], recent=[])               # IG says published, but we can't find it
    out = reconcile_uncertain(db, NOW, lambda acc, **k: ig)
    assert out["alerted"] == 1
    assert db.get(Post, p.id).status == "UNKNOWN" and q.status == "HELD"
    al = db.query(Alert).filter_by(kind="PUBLISH_UNCERTAIN").one()
    assert al.severity == "CRITICAL" and _outbox("PUBLISH_UNCERTAIN")


# ═════════════════════════════════════════════════════════════════════════════
# 2. token leaks
# ═════════════════════════════════════════════════════════════════════════════

def fake_secret(prefix: str = "", n: int = 16) -> str:
    """Secret-shaped test values built at run time, so no full secret pattern lives in the source
    (keeps secret scanners quiet). Prefixes are passed in pieces by the callers for the same reason."""
    return prefix + secrets.token_hex(n)


SECRET_TOKEN = fake_secret("IG" + "AA")          # shape of an Instagram user token


def test_redact_patterns():
    from redact import redact
    auth_code = fake_secret("AQ" + "B", 12)
    client_secret = fake_secret(n=8)
    bearer = fake_secret("EA" + "AG", 10)
    amz_cred = fake_secret("AK" + "IA", 8).upper() + "/x"
    amz_sig = fake_secret(n=6)
    s = redact(f"GET /media?access_token={SECRET_TOKEN}&caption=x code={auth_code} "
               f"client_secret={client_secret} Authorization: OAuth {bearer} "
               f"https://r2/x.mp4?X-Amz-Credential={amz_cred}&X-Amz-Signature={amz_sig} error code=190")
    for leak in (SECRET_TOKEN, auth_code, client_secret, bearer, amz_sig, amz_cred):
        assert leak not in s
    assert "code=190" in s                       # short error codes stay readable


def test_token_in_requests_exception_never_reaches_log_post_or_alert(db, queued, monkeypatch):
    import requests
    from instagram.client import client_for
    a, v, q = queued
    a.ig_access_token = encrypt(SECRET_TOKEN)
    a.consecutive_failures = 2                    # third failure → pause alert containing the error
    db.commit()

    def boom(method, url, **kw):
        raise requests.exceptions.ConnectionError(
            f"HTTPSConnectionPool(host='graph.instagram.com', port=443): Max retries exceeded with url: "
            f"/v25.0/{a.ig_user_id}/content_publishing_limit?fields=quota_usage&access_token={SECRET_TOKEN}")
    monkeypatch.setattr(requests, "request", boom)
    res = publish_item(db, q, now=NOW, host=FakeHost(), sleep=lambda s: None,
                       client_factory=lambda acc, **k: client_for(acc, dry_run=False, sleep=lambda s: None, **k))
    assert res == "PAUSED"
    p = db.query(Post).one()
    texts = [p.error_message, q.error_message, db.get(OwnedChannels, a.id).pause_reason]
    texts += [x.message for x in db.query(Alert).all()]
    for f in glob.glob(os.path.join(os.environ["LOG_DIR"], "**", "*"), recursive=True):
        if os.path.isfile(f):
            texts.append(open(f, encoding="utf-8", errors="replace").read())
    assert "network" in p.error_message
    assert all(SECRET_TOKEN not in (t or "") for t in texts)


def test_oauth_network_error_is_redacted(monkeypatch):
    import requests
    from instagram import oauth
    auth_code, client_secret = fake_secret("AQ" + "D", 12), fake_secret(n=10)
    monkeypatch.setattr(requests, "request", lambda *a, **k: (_ for _ in ()).throw(
        requests.exceptions.ConnectionError(f"url: /oauth/access_token?code={auth_code}&"
                                            f"client_secret={client_secret}")))
    with pytest.raises(E.IGError) as e:
        oauth._http("POST", oauth.CODE_EXCHANGE_URL)
    assert auth_code not in str(e.value) and client_secret not in str(e.value)


# ═════════════════════════════════════════════════════════════════════════════
# 3. comment-based blacklisting
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def commented(db):
    set_llm(MockLLM())
    a = account(db, "cm")
    src = source(db, url="https://www.youtube.com/@RealPod", status="ACTIVE")
    v = video(db, a, source_id=src.id)
    p = post(db, a, v, published_at=NOW - dt.timedelta(hours=3))
    p.source_id = src.id
    db.commit()
    yield a, src, p
    set_llm(None)


def _say(db, a, p, user, text, i=[0]):
    i[0] += 1
    db.add(Comment(ig_comment_id=f"fx{i[0]}", post_id=p.id, owned_channel_id=a.id, username=user, text=text,
                   commented_at=NOW - dt.timedelta(hours=1), status="NEW"))
    db.commit()
    cr.process_new(db, NOW, random.Random(1), client_factory=lambda acc: FakeIG())


def test_single_you_stole_this_keeps_source_active(db, commented):
    a, src, p = commented
    _say(db, a, p, "viewer1", "you stole this")
    assert db.query(Comment).one().status == "RISKY"
    assert db.get(Source, src.id).status == "ACTIVE"
    assert db.query(Alert).filter_by(kind="RISKY_COMMENT").count() == 1


def test_three_different_users_copyright_blacklist(db, commented):
    a, src, p = commented
    _say(db, a, p, "u1", "this is copyright infringement")
    _say(db, a, p, "u1", "copyright!! again")                   # same user twice counts once
    _say(db, a, p, "u2", "DMCA incoming")
    assert db.get(Source, src.id).status == "ACTIVE"
    _say(db, a, p, "u3", "reported for copyright")
    assert db.get(Source, src.id).status == "BLACKLISTED"


def test_casual_ownership_words_do_not_count(db, commented):
    a, src, p = commented
    for u in ("x1", "x2", "x3", "x4"):
        _say(db, a, p, u, "bro stole my video idea lol")
    assert db.get(Source, src.id).status == "ACTIVE"


def test_claim_from_source_owner_blacklists(db, commented):
    a, src, p = commented
    _say(db, a, p, "real.pod", "this is my clip, take it down")
    s = db.get(Source, src.id)
    assert s.status == "BLACKLISTED" and "owner" in s.status_reason


# ═════════════════════════════════════════════════════════════════════════════
# 4. slow processing
# ═════════════════════════════════════════════════════════════════════════════

def test_not_ready_is_not_a_failure(db, queued):
    a, v, q = queued
    a.consecutive_failures = 2
    db.commit()
    ig = FakeIG(status_seq=["IN_PROGRESS"])
    assert _pub(db, q, ig, poll_timeout=0) == "RETRY_SLOW"
    acc = db.get(OwnedChannels, a.id)
    assert acc.status == "ACTIVE" and acc.consecutive_failures == 2
    assert q.status == "PENDING" and q.retry_count == 1 and q.scheduled_for == NOW + dt.timedelta(minutes=30)
    assert "publish" not in ig.calls


# ═════════════════════════════════════════════════════════════════════════════
# 5. R2 presigned URLs
# ═════════════════════════════════════════════════════════════════════════════

class FakeS3:
    def __init__(self):
        self.calls = []

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.calls.append(("upload", bucket, key))

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.calls.append(("presign", op, Params["Key"], ExpiresIn))
        return f"https://acc.r2.cloudflarestorage.com/{Params['Bucket']}/{Params['Key']}?X-Amz-Signature=abc"


@pytest.fixture
def r2_env(monkeypatch):
    for k, v in {"R2_ENDPOINT_URL": "https://acc.r2.cloudflarestorage.com", "R2_ACCESS_KEY_ID": "id",
                 "R2_SECRET_ACCESS_KEY": fake_secret(n=12), "R2_BUCKET": "reels", "R2_PUBLIC_BASE_URL": ""}.items():
        monkeypatch.setenv(k, v)


def test_presigned_default_needs_no_public_base(r2_env, tmp_path):
    from instagram import hosting
    f = tmp_path / "r.mp4"
    f.write_bytes(b"x")
    s3 = FakeS3()
    out = hosting.upload_temp(str(f), dry_run=False, client=s3)
    assert hosting.url_mode() == "presigned" and hosting.configured()
    assert ("presign", "get_object", out["key"], 3600) in s3.calls and "X-Amz-Signature" in out["url"]


def test_public_mode_requires_base_url(r2_env, tmp_path, monkeypatch):
    from instagram import hosting
    f = tmp_path / "r.mp4"
    f.write_bytes(b"x")
    monkeypatch.setenv("R2_URL_MODE", "public")
    with pytest.raises(hosting.HostingNotConfigured):
        hosting.upload_temp(str(f), dry_run=False, client=FakeS3())
    monkeypatch.setenv("R2_PUBLIC_BASE_URL", "https://cdn.example")
    out = hosting.upload_temp(str(f), dry_run=False, client=FakeS3())
    assert out["url"] == f"https://cdn.example/{out['key']}"


# ═════════════════════════════════════════════════════════════════════════════
# 6. background music switch
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("enabled,expect_bgm", [("false", False), ("true", True)])
def test_bg_music_switch(monkeypatch, tmp_path, enabled, expect_bgm):
    import ffmpeg_renderer as fr
    from synthetic_media import make_tone
    bgm = make_tone(str(tmp_path / "bgm.mp3"), duration=3)
    seen = {}
    monkeypatch.setenv("BG_MUSIC_ENABLED", enabled)
    monkeypatch.setattr(fr, "probe_duration", lambda p: 60.0)
    monkeypatch.setattr(fr, "has_audio_stream", lambda p: True)
    monkeypatch.setattr(fr, "_run_ffmpeg", lambda args, label, timeout=600: seen.setdefault("args", args) or True)
    info = fr.render_base("src.mp4", 1.0, 23.0, str(tmp_path / "o.mp4"), bgm_path=bgm)
    assert info["has_bgm"] is expect_bgm
    assert (bgm in seen["args"]) is expect_bgm


def test_bg_music_default_off():
    from config import settings
    assert settings.BG_MUSIC_ENABLED is False


# ═════════════════════════════════════════════════════════════════════════════
# 7. ffmpeg version
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,ok", [
    ("ffmpeg version 6.1.1 Copyright", False),
    ("ffmpeg version 7.0.2-essentials_build", True),
    ("ffmpeg version n7.1 Copyright", True),
    ("ffmpeg version 2026-08-03-git-01a\nlibavfilter    12.  3.101 / 12.  3.101", True),
    ("ffmpeg version 2023-01-01-git-x\nlibavfilter     9. 12.100 /  9. 12.100", False),
    ("garbage", False),
])
def test_ffmpeg_version_parsing(text, ok):
    from media_utils import ffmpeg_version_ok
    assert ffmpeg_version_ok(text)[0] is ok


def test_installed_ffmpeg_passes():
    from media_utils import ffmpeg_version_ok
    assert ffmpeg_version_ok()[0]


def test_startup_stops_on_old_ffmpeg(db):
    import cycle_runner
    with pytest.raises(cycle_runner.StartupCheckFailed):
        cycle_runner.startup_checks(db, ffmpeg_check=lambda: (False, "ffmpeg 6.1 (need >= 7.0)"))
    a = db.query(Alert).filter_by(kind="FFMPEG_TOO_OLD").one()
    assert a.severity == "CRITICAL" and "6.1" in a.message


# ═════════════════════════════════════════════════════════════════════════════
# 8. separate venv + PO token provider (scripts are not executed, only checked)
# ═════════════════════════════════════════════════════════════════════════════

def _read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_setup_venv_script_contents():
    s = _read("scripts/setup_venv.ps1")
    for needle in ("-m\", \"venv\"", "download.pytorch.org/whl/cu118", "requirements.txt",
                   "\"-U\", \"yt-dlp[default]\", \"yt-dlp-ejs\", \"bgutil-ytdlp-pot-provider\"",
                   "bgutil-ytdlp-pot-provider.git", "npm", "tsc"):
        assert needle in s, needle
    assert all(ord(c) < 128 for c in s)          # Windows PowerShell 5.1 reads BOM-less files as ANSI


def test_autostart_uses_venv_and_starts_pot_provider():
    s = _read("scripts/install_autostart.ps1")
    assert ".venv\\Scripts\\pythonw.exe" in s and "Reels-PotProvider" in s and "build\\main.js" in s
    assert "Python311\\pythonw.exe" not in s
    assert all(ord(c) < 128 for c in s)


@pytest.mark.parametrize("script", ["scripts/setup_venv.ps1", "scripts/install_autostart.ps1"])
def test_powershell_scripts_parse(script):
    ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                      "powershell.exe")
    if not os.path.exists(ps):
        pytest.skip("Windows PowerShell not available")
    cmd = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
           f"'{os.path.join(ROOT, script)}', [ref]$null, [ref]$e); $e.Count")
    out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True,
                         timeout=60).stdout.strip()
    assert out == "0"


def test_downloader_uses_po_token_clients_and_provider(monkeypatch, tmp_path):
    """Settings are forwarded to yt-dlp (fallback combination: mweb + bgutil server)."""
    import downloader
    seen = {}

    class R:
        returncode, stderr = 1, "ERROR: stop here"
    monkeypatch.setenv("POT_PROVIDER_URL", "http://127.0.0.1:4417")
    monkeypatch.setenv("YTDLP_PLAYER_CLIENT", "mweb")
    monkeypatch.setattr(downloader.subprocess, "run", lambda cmd, **kw: seen.setdefault("cmd", cmd) and R())
    with pytest.raises(downloader.DownloadError):
        downloader.download_youtube_video("https://www.youtube.com/watch?v=abcdefgh", out_dir=str(tmp_path))
    cmd = " ".join(seen["cmd"])
    assert "youtube:player_client=mweb" in cmd
    assert "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4417" in cmd


def _captured_base(monkeypatch):
    import downloader
    seen = {}

    class R:
        returncode, stdout, stderr = 0, '{"entries": []}', ""
    monkeypatch.setattr(downloader.subprocess, "run", lambda cmd, **kw: seen.setdefault("cmd", cmd) and R())
    downloader.ytdlp_json("ytsearch3:podcast", ["--flat-playlist"])
    return seen["cmd"]


def test_verified_default_combination(monkeypatch, tmp_path):
    """Default = the combination verified on 2026-10-22: venv yt-dlp, deno by full path, yt-dlp's own
    default player clients, no cookies, no forced remote components."""
    import sys

    import downloader
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv("COOKIES_PATH", str(cookie))
    venv_py = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    cmd = _captured_base(monkeypatch)
    joined = " ".join(cmd)
    assert cmd[0] == (venv_py if os.path.exists(venv_py) else sys.executable)
    assert "player_client" not in joined and "--cookies" not in joined and "--remote-components" not in joined
    rt = cmd[cmd.index("--js-runtimes") + 1]
    assert rt.startswith("deno")
    deno = os.path.join(ROOT, "tools", "deno", "deno.exe")
    if os.path.exists(deno):
        assert rt == f"deno:{deno}"                  # full path: PATH on this PC is incomplete
    assert downloader.js_runtime_arg() == rt


def test_cookies_and_node_runtime_are_opt_in(monkeypatch, tmp_path):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n")
    custom_py = str(tmp_path / "python.exe")
    monkeypatch.setenv("COOKIES_PATH", str(cookie))
    monkeypatch.setenv("YTDLP_USE_COOKIES", "true")
    monkeypatch.setenv("YTDLP_JS_RUNTIME", "node")
    monkeypatch.setenv("YTDLP_PYTHON", custom_py)
    cmd = _captured_base(monkeypatch)
    assert cmd[0] == custom_py and str(cookie) in cmd
    assert cmd[cmd.index("--js-runtimes") + 1].startswith("node")


def test_update_targets_venv_and_channel(monkeypatch):
    import downloader
    seen = []

    class R:
        returncode, stderr = 0, ""
    monkeypatch.setattr(downloader, "_updated_this_process", False)
    monkeypatch.setenv("YTDLP_UPDATE_CHANNEL", "nightly")
    monkeypatch.setattr(downloader.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or R())
    downloader.update_ytdlp_once()
    downloader.update_ytdlp_once()                    # once per process
    assert len(seen) == 1
    assert seen[0][0] == downloader.ytdlp_python() and "--pre" in seen[0]
    assert "yt-dlp[default]" in seen[0] and "bgutil-ytdlp-pot-provider" in seen[0]


def test_ollama_cli_full_path(monkeypatch, tmp_path):
    from llm_client import ollama_exe
    custom = str(tmp_path / "ollama.exe")
    monkeypatch.setenv("OLLAMA_EXE", custom)
    assert ollama_exe() == custom
    monkeypatch.setenv("OLLAMA_EXE", "")
    exe = ollama_exe()
    assert exe == "ollama" or os.path.isabs(exe)


# ═════════════════════════════════════════════════════════════════════════════
# 9. Ollama model
# ═════════════════════════════════════════════════════════════════════════════

def test_ollama_defaults():
    from config import settings
    assert settings.OLLAMA_MODEL == "qwen3:32b" and settings.OLLAMA_NUM_CTX == 6144


def test_parse_ollama_ps():
    from llm_client import parse_ollama_ps
    out = ("NAME         ID              SIZE     PROCESSOR          UNTIL\n"
           "qwen3:32b    030ee887880f    25 GB    48%/52% CPU/GPU    4 minutes from now\n"
           "qwen3:8b     500a1f067a9f    6 GB     100% GPU           4 minutes from now\n")
    assert parse_ollama_ps(out, "qwen3:32b") == 0.52
    assert parse_ollama_ps(out, "qwen3:8b") == 1.0
    assert parse_ollama_ps(out, "qwen3:30b-a3b") is None


def test_gpu_residency_from_api_ps():
    from llm_client import OllamaClient

    def transport(method, url, payload, timeout):
        assert url.endswith("/api/ps")
        return 200, {"models": [{"name": "qwen3:32b", "size": 20_000, "size_vram": 15_000}]}
    c = OllamaClient(base_url="http://x", model="qwen3:32b", transport=transport, sleep=lambda s: None)
    assert c.gpu_residency() == 0.75


class GpuLLM(MockLLM):
    def __init__(self, share):
        super().__init__()
        self.share, self.events = share, []

    def load(self, keep_alive="5m"):
        self.events.append("load")

    def gpu_residency(self):
        return self.share

    def unload(self):
        self.events.append("unload")


@pytest.mark.parametrize("share,alarm", [(0.6, True), (1.0, False)])
def test_startup_warns_when_model_spills_to_cpu(db, monkeypatch, share, alarm):
    import cycle_runner
    monkeypatch.setenv("LLM_BACKEND", "ollama")
    llm = GpuLLM(share)
    out = cycle_runner.startup_checks(db, llm=llm, ffmpeg_check=lambda: (True, "ok"))
    assert out["gpu_residency"] == share and llm.events == ["load", "unload"]
    rows = db.query(Alert).filter_by(kind="LLM_CPU_OFFLOAD").all()
    assert bool(rows) is alarm
    if alarm:
        assert rows[0].severity == "WARNING" and "60%" in rows[0].message
