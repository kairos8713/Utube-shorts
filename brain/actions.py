"""
brain/actions.py — the ONLY things Qwen may propose, with validators and appliers.

Whitelist (task-scoped further in brain/core.run_decision):
  source_add, source_trial, source_remove, source_blacklist,
  slot_assign, account_pace, account_pause,
  comment_reply, comment_hide, report_write, noop
(clip scoring, caption writing and cover choice are content tasks handled in
 qwen_specialized / cover_frame and logged as decisions.)

Never allowed — there is intentionally NO applier for these anywhere in the code:
  password / e-mail / 2FA changes, deleting posts or accounts, revoking tokens /
  disconnecting accounts, changing guardrails, settings or code, re-activating a
  paused account (only Emre can resume, from the panel).
"""
import json
import re

import guardrails
from timeutil import utcnow

FORBIDDEN_ACTIONS = frozenset({
    "change_password", "change_email", "change_2fa", "disable_2fa", "delete_post", "delete_media",
    "delete_account", "revoke_token", "disconnect_account", "change_guardrail", "change_settings",
    "modify_code", "account_resume", "account_activate", "unpause", "set_emergency_stop",
    "clear_emergency_stop", "unblacklist_source",
})


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


# ── validators: (session, action, ctx) -> list[str] ──────────────────────────

def _v_account(session, a, ctx, need_status=None):
    from models import OwnedChannels
    if not _int(a.get("account_id")):
        return None, ["account_id must be an integer"]
    acc = session.get(OwnedChannels, a["account_id"])
    if acc is None:
        return None, [f"account {a['account_id']} not found"]
    allowed_ids = ctx.get("account_ids")
    if allowed_ids is not None and acc.id not in allowed_ids:
        return None, [f"account {acc.id} is outside this task's scope"]
    return acc, []


def v_account_pace(session, a, ctx):
    acc, errs = _v_account(session, a, ctx)
    if errs:
        return errs
    return guardrails.validate_cap_change(session, acc, a.get("daily_post_cap"))


def v_account_pause(session, a, ctx):
    acc, errs = _v_account(session, a, ctx)
    if errs:
        return errs
    if not isinstance(a.get("reason"), str) or len(a["reason"].strip()) < 5:
        return ["pause needs a reason"]
    if acc.status in ("PAUSED", "PAUSED_AUTO"):
        return ["account already paused"]
    return []


def v_slot_assign(session, a, ctx):
    acc, errs = _v_account(session, a, ctx)
    if errs:
        return errs
    slots = a.get("slots")
    valid = set(ctx.get("valid_slots", []))
    if not isinstance(slots, list) or not (1 <= len(slots) <= 4):
        return ["slots must be a list of 1-4 slot keys"]
    bad = [s for s in slots if not _int(s) or (valid and s not in valid)]
    return [f"invalid slot keys {bad}; valid: {sorted(valid)}"] if bad else []


def v_source_add(session, a, ctx):
    from models import Source
    url = a.get("channel_url")
    if not isinstance(url, str) or not re.match(r"^https://www\.youtube\.com/(@[\w.-]+|channel/[\w-]+)/?$", url):
        return ["channel_url must be https://www.youtube.com/@handle or /channel/ID"]
    if session.query(Source).filter_by(channel_url=url.rstrip("/")).first():
        return ["source already in pool"]
    if not _int(a.get("sector_id")):
        return ["sector_id must be an integer"]
    return []


def _v_source(session, a, ctx, from_status):
    from models import Source
    if not _int(a.get("source_id")):
        return None, ["source_id must be an integer"]
    src = session.get(Source, a["source_id"])
    if src is None:
        return None, [f"source {a['source_id']} not found"]
    if src.status not in from_status:
        return None, [f"source status {src.status} cannot change this way"]
    return src, []


def v_source_trial(session, a, ctx):
    return _v_source(session, a, ctx, {"CANDIDATE"})[1]


def v_source_remove(session, a, ctx):
    return _v_source(session, a, ctx, {"CANDIDATE", "TRIAL", "ACTIVE"})[1]


def v_source_blacklist(session, a, ctx):
    errs = _v_source(session, a, ctx, {"CANDIDATE", "TRIAL", "ACTIVE", "REJECTED"})[1]
    if not errs and not isinstance(a.get("reason"), str):
        errs = ["blacklist needs a reason"]
    return errs


def v_comment_reply(session, a, ctx):
    if not _int(a.get("comment_id")) or a["comment_id"] != ctx.get("comment_id"):
        return ["comment_id must be the comment given in this task"]
    if not isinstance(a.get("text"), str) or not a["text"].strip():
        return ["text required"]
    return []


def v_comment_hide(session, a, ctx):
    if not _int(a.get("comment_id")) or a["comment_id"] != ctx.get("comment_id"):
        return ["comment_id must be the comment given in this task"]
    return []


def v_report_write(session, a, ctx):
    if not isinstance(a.get("narrative"), str) or len(a["narrative"]) > 6000:
        return ["narrative must be a string ≤ 6000 chars"]
    return []


def v_noop(session, a, ctx):
    return []


# ── appliers ─────────────────────────────────────────────────────────────────

def ap_account_pace(session, a, ctx):
    from models import OwnedChannels
    acc = session.get(OwnedChannels, a["account_id"])
    acc.daily_post_cap = a["daily_post_cap"]


def ap_account_pause(session, a, ctx):
    from models import OwnedChannels
    acc = session.get(OwnedChannels, a["account_id"])
    acc.status = "PAUSED_AUTO"
    acc.pause_reason = f"qwen: {a['reason'][:300]}"
    acc.paused_at = utcnow()


def ap_slot_assign(session, a, ctx):
    import system_state
    system_state.put(f"slot_plan:{a['account_id']}", json.dumps({"slots": a["slots"], "at": utcnow().isoformat()}),
                     session)


def ap_source_add(session, a, ctx):
    from credit import handle_from_url
    from models import Source
    url = a["channel_url"].rstrip("/")
    session.add(Source(channel_url=url, handle=handle_from_url(url), sector_id=a["sector_id"],
                       status="CANDIDATE", status_reason="qwen discovery", status_changed_at=utcnow()))


def _set_source(session, a, status, reason):
    from models import Source
    src = session.get(Source, a["source_id"])
    src.status = status
    src.status_reason = reason[:300]
    src.status_changed_at = utcnow()


def ap_source_trial(session, a, ctx):
    _set_source(session, a, "TRIAL", a.get("reason") or "qwen: trial")


def ap_source_remove(session, a, ctx):
    _set_source(session, a, "REJECTED", a.get("reason") or "qwen: removed")


def ap_source_blacklist(session, a, ctx):
    _set_source(session, a, "BLACKLISTED", a.get("reason") or "qwen: blacklisted")


def ap_ctx_store(session, a, ctx):
    """Content actions (reply / hide / report): the calling job executes them."""
    ctx.setdefault("accepted", []).append(a)


ACTIONS = {
    "account_pace": (v_account_pace, ap_account_pace),
    "account_pause": (v_account_pause, ap_account_pause),
    "slot_assign": (v_slot_assign, ap_slot_assign),
    "source_add": (v_source_add, ap_source_add),
    "source_trial": (v_source_trial, ap_source_trial),
    "source_remove": (v_source_remove, ap_source_remove),
    "source_blacklist": (v_source_blacklist, ap_source_blacklist),
    "comment_reply": (v_comment_reply, ap_ctx_store),
    "comment_hide": (v_comment_hide, ap_ctx_store),
    "report_write": (v_report_write, ap_ctx_store),
    "noop": (v_noop, lambda s, a, c: None),
}

assert not FORBIDDEN_ACTIONS & set(ACTIONS), "a forbidden action must never have an applier"
