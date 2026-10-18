"""
web/views.py — control panel pages (all behind login, all POSTs CSRF-protected).

  /                 alarms (red), EMERGENCY STOP (typed confirmation), system status
  /accounts         list, pause / resume (resume = Emre only), connect (OAuth),
                    setup date, token expiry, last posts, relative-performance chart
  /today            today's plan in Warsaw time, slot source
  /sources          pool + manual blacklist
  /reports          cycle reports (+ archive)
  /decisions        filterable Qwen decision log
  /comments         risky comments + reply history
  /guardrails       read-only limits
"""
import datetime as _dt
import json
import shutil

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

import guardrails
import system_state
from alerts import open_alerts, resolve_alert
from analytics import relative
from config import ROOT_DIR, settings
from gpu_lock import current_holder
from log_setup import get_logger
from models import (Alert, Comment, CommentReply, CycleReport, Decision, OwnedChannels, Post, Sectors, Session,
                    Source, UploadQueue)
from timeutil import fmt_local, to_local, utcnow
from web.auth import login_required

log = get_logger("panel")
main_bp = Blueprint("main", __name__)

SERVICES = {"cycle_runner": 15, "publisher": 5, "comment_responder": 5, "watchdog": 5}


def _db():
    return Session()


# ── home ─────────────────────────────────────────────────────────────────────

def system_status(s):
    now = utcnow()
    services = []
    for name, max_age in SERVICES.items():
        hb = system_state.last_heartbeat(name, s)
        age = (now - hb).total_seconds() / 60 if hb else None
        services.append({"name": name, "last": fmt_local(hb) if hb else "hiç", "ok": age is not None and age <= max_age,
                         "age_min": round(age, 1) if age is not None else None})
    du = shutil.disk_usage(ROOT_DIR)
    return {
        "services": services,
        "gpu": current_holder(),
        "queue": {st: s.query(UploadQueue).filter_by(status=st).count() for st in ("PENDING", "HELD", "FAILED")},
        "disk_free_pct": round(du.free / du.total * 100, 1),
        "disk_free_gb": round(du.free / 1e9, 1),
        "dry_run": settings.DRY_RUN,
        "emergency": system_state.emergency_stopped(s),
    }


@main_bp.route("/")
@login_required
def home():
    s = _db()
    try:
        return render_template("home.html", alerts=open_alerts(s), status=system_status(s))
    finally:
        s.close()


@main_bp.route("/emergency", methods=["POST"])
@login_required
def emergency():
    s = _db()
    try:
        action = request.form.get("action")
        if action == "stop" and request.form.get("confirm") == "DURDUR":
            system_state.set_emergency_stop(True, by="panel", session=s)
            flash("ACİL DURDURMA etkin: hiçbir dış eylem yapılmayacak.", "danger")
        elif action == "clear" and request.form.get("confirm") == "DEVAM":
            system_state.set_emergency_stop(False, by="panel", session=s)
            flash("Acil durdurma kaldırıldı.", "info")
        else:
            flash("Onay metni hatalı — işlem yapılmadı.", "warning")
    finally:
        s.close()
    return redirect(url_for("main.home"))


@main_bp.route("/alerts/<int:alert_id>/resolve", methods=["POST"])
@login_required
def alert_resolve(alert_id):
    resolve_alert(alert_id)
    return redirect(request.referrer or url_for("main.home"))


# ── accounts ─────────────────────────────────────────────────────────────────

def _spark(values, w=160, h=36):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    hi = max(max(vals), 1.0) * 1.1
    step = w / (len(vals) - 1)
    pts = " ".join(f"{i * step:.1f},{h - v / hi * h:.1f}" for i, v in enumerate(vals))
    base = h - 1.0 / hi * h
    return (f'<svg width="{w}" height="{h}" role="img" aria-label="relative views">'
            f'<line x1="0" y1="{base:.1f}" x2="{w}" y2="{base:.1f}" stroke="currentColor" stroke-opacity=".25" stroke-dasharray="3 3"/>'
            f'<polyline points="{pts}" fill="none" stroke="currentColor" stroke-width="2"/></svg>')


@main_bp.route("/accounts")
@login_required
def accounts():
    s = _db()
    try:
        rows = []
        cache = {}
        for a in s.query(OwnedChannels).order_by(OwnedChannels.id).all():
            posts = (s.query(Post).filter_by(owned_channel_id=a.id, status="PUBLISHED")
                     .order_by(Post.published_at.desc()).limit(20).all())
            rel = [relative(s, p, 24, _median_cache=cache) for p in reversed(posts)]
            ok, why = guardrails.warmup_ok(a)
            rows.append({"a": a, "sector": s.get(Sectors, a.sector_id), "posts": posts[:5], "spark": _spark(rel),
                         "warmup": "hazır" if ok else why, "cap": guardrails.effective_cap(s, a),
                         "pending": s.query(UploadQueue).filter_by(owned_channel_id=a.id, status="PENDING").count(),
                         "held": s.query(UploadQueue).filter_by(owned_channel_id=a.id, status="HELD").count()})
        return render_template("accounts.html", rows=rows, sectors=s.query(Sectors).all(),
                               oauth_ready=bool(settings.IG_APP_ID and settings.IG_APP_SECRET))
    finally:
        s.close()


@main_bp.route("/accounts/new", methods=["POST"])
@login_required
def account_new():
    from crypto_utils import encrypt
    s = _db()
    try:
        name = (request.form.get("name") or "").strip()
        niche = (request.form.get("niche") or "").strip()
        if not name or not niche:
            flash("Hesap adı ve niş gerekli.", "warning")
            return redirect(url_for("main.accounts"))
        sec = s.query(Sectors).filter_by(name=niche).first()
        if not sec:
            from media_utils import music_slug
            sec = Sectors(name=niche, title=niche, description=niche, tags=niche, music_slug=music_slug(niche))
            s.add(sec)
            s.flush()
        s.add(OwnedChannels(name=name, ig_username=name, sector_id=sec.id, level=1, mail="-", password=encrypt("-"),
                            status="DISCONNECTED", daily_post_cap=guardrails.DAILY_CAP_START))
        s.commit()
        flash(f"Hesap eklendi: {name}. Şimdi 'Hesap Bağla' ile Instagram'a bağlayın.", "info")
    finally:
        s.close()
    return redirect(url_for("main.accounts"))


@main_bp.route("/accounts/<int:acc_id>/setup", methods=["POST"])
@login_required
def account_setup(acc_id):
    s = _db()
    try:
        a = s.get(OwnedChannels, acc_id) or abort(404)
        raw = request.form.get("setup_completed_at", "")
        try:
            local = _dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M")
        except ValueError:
            flash("Tarih formatı hatalı.", "warning")
            return redirect(url_for("main.accounts"))
        from timeutil import local_to_utc
        a.ig_setup_completed_at = local_to_utc(local)
        if a.status == "DISCONNECTED" and a.ig_access_token:
            a.status = "WARMUP"
        s.commit()
        flash(f"Kurulum tarihi kaydedildi; ilk paylaşım en erken {fmt_local(a.ig_setup_completed_at + guardrails.WARMUP)}.", "info")
    finally:
        s.close()
    return redirect(url_for("main.accounts"))


@main_bp.route("/accounts/<int:acc_id>/pause", methods=["POST"])
@login_required
def account_pause(acc_id):
    s = _db()
    try:
        a = s.get(OwnedChannels, acc_id) or abort(404)
        a.status, a.pause_reason, a.paused_at = "PAUSED", "Emre (panel)", utcnow()
        s.query(UploadQueue).filter_by(owned_channel_id=a.id, status="PENDING").update({"status": "HELD"})
        s.commit()
        flash(f"{a.ig_username or a.name} duraklatıldı.", "info")
    finally:
        s.close()
    return redirect(url_for("main.accounts"))


@main_bp.route("/accounts/<int:acc_id>/resume", methods=["POST"])
@login_required
def account_resume(acc_id):
    """The ONLY path that re-activates a paused account."""
    import scheduler
    s = _db()
    try:
        a = s.get(OwnedChannels, acc_id) or abort(404)
        if a.status not in ("PAUSED", "PAUSED_AUTO"):
            flash("Hesap duraklatılmış değil.", "warning")
            return redirect(url_for("main.accounts"))
        if not a.ig_access_token:
            flash("Önce hesabı bağlayın.", "warning")
            return redirect(url_for("main.accounts"))
        a.status, a.pause_reason, a.consecutive_failures = "ACTIVE", None, 0
        s.query(UploadQueue).filter_by(owned_channel_id=a.id, status="HELD").update({"status": "PENDING"})
        s.commit()
        scheduler.reschedule_missed(s)
        log.info("account %s resumed by Emre", a.id)
        flash(f"{a.ig_username or a.name} devam ediyor.", "info")
    finally:
        s.close()
    return redirect(url_for("main.accounts"))


# ── OAuth ────────────────────────────────────────────────────────────────────

@main_bp.route("/oauth/instagram/start/<int:acc_id>")
@login_required
def oauth_start(acc_id):
    from instagram import oauth
    state = oauth.new_state()
    session["ig_oauth"] = {"state": state, "account_id": acc_id}
    return redirect(oauth.authorize_url(state))


def _finish_oauth(code, state):
    from instagram import oauth
    from instagram.client import IGClient
    pending = session.get("ig_oauth") or {}
    if not code or not state or state != pending.get("state"):
        flash("OAuth durumu doğrulanamadı (state).", "danger")
        return redirect(url_for("main.accounts"))
    s = _db()
    try:
        a = s.get(OwnedChannels, pending["account_id"]) or abort(404)
        oauth.complete_login(s, a, code, me_fn=lambda tok: IGClient(tok, "", dry_run=False).me("user_id,username"))
        session.pop("ig_oauth", None)
        flash(f"Bağlandı: @{a.ig_username}. Kurulum tarihini girmeyi unutmayın.", "info")
    except Exception as e:
        flash(f"Bağlantı başarısız: {e}", "danger")
    finally:
        s.close()
    return redirect(url_for("main.accounts"))


@main_bp.route("/oauth/instagram/callback")
@login_required
def oauth_callback():
    return _finish_oauth(request.args.get("code"), request.args.get("state"))


@main_bp.route("/oauth/instagram/paste", methods=["POST"])
@login_required
def oauth_paste():
    """Fallback when the HTTPS redirect cannot reach the local panel: paste the final URL."""
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(request.form.get("url", "")).query)
    return _finish_oauth((q.get("code") or [""])[0], (q.get("state") or [""])[0])


# ── today / sources / reports / decisions / comments / guardrails ────────────

@main_bp.route("/today")
@login_required
def today():
    import scheduler
    s = _db()
    try:
        names = {a.id: a.ig_username or a.name for a in s.query(OwnedChannels).all()}
        day = to_local(utcnow()).date()
        return render_template("today.html", rows=scheduler.plan_for_display(s, day), names=names, day=day)
    finally:
        s.close()


@main_bp.route("/sources")
@login_required
def sources():
    s = _db()
    try:
        st = request.args.get("status")
        q = s.query(Source)
        if st:
            q = q.filter_by(status=st)
        rows = q.order_by(Source.status, Source.score.desc()).all()
        return render_template("sources.html", rows=rows, status=st, sectors={x.id: x.name for x in s.query(Sectors)})
    finally:
        s.close()


@main_bp.route("/sources/<int:src_id>/blacklist", methods=["POST"])
@login_required
def source_blacklist(src_id):
    import sources_manager
    s = _db()
    try:
        sources_manager.blacklist_source(s, src_id, "Emre (panel)")
    finally:
        s.close()
    return redirect(url_for("main.sources"))


@main_bp.route("/reports")
@main_bp.route("/reports/<int:rep_id>")
@login_required
def reports(rep_id=None):
    s = _db()
    try:
        archive = s.query(CycleReport).order_by(CycleReport.id.desc()).limit(100).all()
        rep = s.get(CycleReport, rep_id) if rep_id else (archive[0] if archive else None)
        stats = json.loads(rep.stats_json) if rep and rep.stats_json else None
        return render_template("reports.html", rep=rep, stats=stats, archive=archive)
    finally:
        s.close()


@main_bp.route("/decisions")
@login_required
def decisions():
    s = _db()
    try:
        q = s.query(Decision)
        task, applied, acc = request.args.get("task"), request.args.get("applied"), request.args.get("account")
        if task:
            q = q.filter_by(task=task)
        if applied in ("1", "0"):
            q = q.filter_by(applied=applied == "1")
        if acc:
            q = q.filter_by(owned_channel_id=int(acc))
        tasks = [t for (t,) in s.query(Decision.task).distinct().all()]
        return render_template("decisions.html", rows=q.order_by(Decision.id.desc()).limit(300).all(),
                               tasks=sorted(tasks), f={"task": task, "applied": applied, "account": acc})
    finally:
        s.close()


@main_bp.route("/comments")
@login_required
def comments():
    s = _db()
    try:
        risky = (s.query(Comment).filter_by(needs_attention=True).order_by(Comment.attention_resolved,
                                                                           Comment.id.desc()).limit(200).all())
        replies = s.query(CommentReply).order_by(CommentReply.id.desc()).limit(200).all()
        texts = {c.id: c.text for c in s.query(Comment).filter(Comment.id.in_([r.comment_id for r in replies])).all()}
        return render_template("comments.html", risky=risky, replies=replies, texts=texts)
    finally:
        s.close()


@main_bp.route("/comments/<int:cid>/resolve", methods=["POST"])
@login_required
def comment_resolve(cid):
    s = _db()
    try:
        c = s.get(Comment, cid) or abort(404)
        c.attention_resolved = True
        s.commit()
    finally:
        s.close()
    return redirect(url_for("main.comments"))


@main_bp.route("/guardrails")
@login_required
def guardrails_page():
    return render_template("guardrails.html", rows=guardrails.describe())
