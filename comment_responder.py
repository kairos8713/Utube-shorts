"""
comment_responder.py — comment polling, classification and replies (service).

* Polling, not webhooks (no public endpoint at home): every ~10 min the
  comments of posts published in the last 48 h are fetched.
* Classification — narrow Qwen call, it can only return a label:
  question | discussion | emoji_short | spam_abuse | risky
  Code overrides: copyright/takedown/ownership words → risky; links/@ → spam.
* Rules: risky → no reply, "Dikkat gerektirenler" + e-mail (copyright-type
  claims also blacklist the source); spam_abuse → hide via API; emoji_short →
  mostly ignored, ~20 % short reply; question/discussion → reply.
* Replies — second narrow call, can only return reply text. Code filter: one
  sentence, no links / @handles / follow CTAs / hashtags, no numbers that are not
  in the clip, not similar to earlier replies. Each reply gets its own random
  40–70 min delay; sending re-checks emergency stop + guardrails.
"""
import datetime as _dt
import random
import re
import time
import traceback

import guardrails
import jobs
import system_state
from alerts import raise_alert
from brain.core import log_decision
from gpu_lock import GPUBusy, gpu_job
from instagram.client import client_for
from instagram.errors import IGError, PAUSE_KINDS
from llm_client import LLMError, get_llm
from log_setup import get_logger
from models import Comment, CommentReply, OwnedChannels, Post, Session, videos
from prompt_safety import data_block, system_prompt
from qwen_specialized import caption_similarity
from timeutil import utcnow

log = get_logger("comments")

LABELS = ("question", "discussion", "emoji_short", "spam_abuse", "risky")
REPLY_PROB = {"question": 1.0, "discussion": 0.8, "emoji_short": 0.2}
REPLY_SIMILARITY_MAX = 0.7
MAX_REPLY_CHARS = 150

RISKY_RE = re.compile(r"(copyright|copy right|dmca|take ?down|remove (this|it)|my (video|content|clip|channel)|"
                      r"stole|stolen|steal|lawyer|sue\b|report(ed|ing)? you|without (my )?permission|credit me|"
                      r"reupload|re-upload|telif)", re.I)
# Only EXPLICIT rights claims count towards automatic blacklisting. "stole" / "my video" stay
# RISKY (human attention) but are too common as casual banter to count as a claim.
CLAIM_RE = re.compile(r"(copyright|copy right|dmca|take ?down|without (my )?permission|re-?upload|telif)", re.I)
BLACKLIST_MIN_CLAIMANTS = 3
BLACKLIST_WINDOW = _dt.timedelta(days=7)
LINK_RE = re.compile(r"(https?://|www\.|\.com\b|\.ly\b|t\.me/|wa\.me/)", re.I)
MENTION_RE = re.compile(r"(^|\s)@[\w.]+")
CTA_RE = re.compile(r"\b(follow|subscribe|link in (my )?bio|dm me|check (out )?my|visit my)\b", re.I)


# ── fetching ─────────────────────────────────────────────────────────────────

def fetch_new(session, now=None, client_factory=client_for) -> int:
    now = now or utcnow()
    added = 0
    accounts = session.query(OwnedChannels).filter(OwnedChannels.ig_access_token != None).all()  # noqa: E711
    for acc in accounts:
        if acc.status == "DISCONNECTED":
            continue
        posts = (session.query(Post).filter(Post.owned_channel_id == acc.id, Post.status == "PUBLISHED",
                                            Post.published_at >= now - _dt.timedelta(hours=48)).all())
        if not posts:
            continue
        client = client_factory(acc)
        for p in posts:
            try:
                rows = client.comments(p.ig_media_id)
            except IGError as e:
                log.warning("comments fetch failed for post %s: %s", p.id, e)
                if e.kind in PAUSE_KINDS:
                    break
                continue
            for c in rows:
                if session.query(Comment).filter_by(ig_comment_id=str(c["id"])).first():
                    continue
                if acc.ig_username and (c.get("username") or "").lower() == acc.ig_username.lower():
                    continue                        # our own replies
                session.add(Comment(ig_comment_id=str(c["id"]), post_id=p.id, owned_channel_id=acc.id,
                                    username=c.get("username"), text=(c.get("text") or "")[:2000],
                                    commented_at=_parse_ts(c.get("timestamp")) or now, fetched_at=now))
                added += 1
    session.commit()
    return added


def _parse_ts(ts):
    if not ts:
        return None
    try:
        d = _dt.datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        return d.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


# ── classification ───────────────────────────────────────────────────────────

_CLASS_ROLE = """You classify ONE Instagram comment left under a short video clip.
Labels: question (asks something), discussion (opinion / argument / substantive reaction),
emoji_short (only emojis or a few words like 'lol', 'fire'), spam_abuse (spam, scams, links,
self-promotion, insults, harassment), risky (copyright or ownership claims, takedown / removal
requests, legal threats, personal or sensitive matters about real people, anything a human
should handle)."""
_CLASS_SPEC = '{"label": "question|discussion|emoji_short|spam_abuse|risky", "reason": "<max 15 words>"}'


def code_label(text: str):
    if RISKY_RE.search(text or ""):
        return "risky"
    if LINK_RE.search(text or "") or MENTION_RE.search(text or ""):
        return "spam_abuse"
    return None


def classify_comment(session, comment, llm=None) -> str:
    llm = llm or get_llm()
    forced = code_label(comment.text)
    label, reason = forced, "code rule"
    if forced != "risky":           # risky by keyword is final; otherwise ask Qwen
        msgs = [{"role": "system", "content": system_prompt(_CLASS_ROLE, _CLASS_SPEC)},
                {"role": "user", "content": data_block("COMMENT", comment.text, 1500) + "\nClassify it."}]
        try:
            obj = llm.chat_json(msgs, task="comment_classify", max_tokens=80,
                                validator=lambda o: [] if o.get("label") in LABELS else ["label must be one of " + "|".join(LABELS)])
            label = forced or obj["label"]
            reason = obj.get("reason", "")
        except LLMError as e:
            label, reason = forced or "emoji_short", f"LLM failed: {e}"   # safe default: no reply
    comment.classification, comment.class_reason = label, str(reason)[:200]
    comment.status = "CLASSIFIED"
    log_decision(session, "comment_classify", {"comment_id": comment.id, "label": label}, "OK", True, reason,
                 input_summary=(comment.text or "")[:200], account_id=comment.owned_channel_id)
    return label


# ── replies ──────────────────────────────────────────────────────────────────

_REPLY_ROLE = """You reply, as the account owner, to ONE comment under a short clip. Write ONE short
natural sentence (max 120 characters) that responds to the comment using only what is in the clip
transcript and caption. Never invent facts, numbers or names that are not in the clip. No links,
no @mentions, no hashtags, no asking people to follow/subscribe, no emojis spam (max 1)."""
_REPLY_SPEC = '{"reply": "<one sentence>"}'


def reply_problems(reply: str, context_text: str, previous: list) -> list:
    errs = []
    r = (reply or "").strip()
    if not r or len(r) > MAX_REPLY_CHARS:
        errs.append(f"reply must be 1-{MAX_REPLY_CHARS} characters")
    if len([s for s in re.split(r"[.!?]+(?:\s|$)", r) if s.strip()]) > 1:
        errs.append("exactly one sentence")
    if LINK_RE.search(r):
        errs.append("no links")
    if MENTION_RE.search(r):
        errs.append("no @mentions")
    if CTA_RE.search(r):
        errs.append("no follow/subscribe calls")
    if "#" in r:
        errs.append("no hashtags")
    nums = re.findall(r"\d[\d.,]*", r)
    if any(n.strip(".,") not in (context_text or "") for n in nums):
        errs.append("numbers not present in the clip")
    if any(caption_similarity(r, p) >= REPLY_SIMILARITY_MAX for p in previous):
        errs.append("too similar to an earlier reply")
    return errs


def write_reply(session, comment, llm=None):
    llm = llm or get_llm()
    post = session.get(Post, comment.post_id)
    vid = session.get(videos, post.video_id)
    context = f"{vid.transcript or ''}\n{vid.caption or ''}"
    previous = [r.text for r in session.query(CommentReply).filter_by(owned_channel_id=comment.owned_channel_id)
                .order_by(CommentReply.id.desc()).limit(100).all()]
    user = "\n\n".join([data_block("CLIP_TRANSCRIPT", vid.transcript or "", 3000),
                        data_block("CAPTION", vid.caption or "", 800),
                        data_block("COMMENT", comment.text, 1000),
                        "Write the reply JSON."])
    msgs = [{"role": "system", "content": system_prompt(_REPLY_ROLE, _REPLY_SPEC)},
            {"role": "user", "content": user}]
    try:
        obj = llm.chat_json(msgs, task="comment_reply", max_tokens=80, temperature=0.8,
                            validator=lambda o: reply_problems(o.get("reply", ""), context, previous))
        return obj["reply"].strip(), None
    except LLMError as e:
        return None, str(e)


def _norm_handle(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower().lstrip("@"))


def maybe_blacklist_source(session, comment, post, now=None):
    """A single risky comment never blacklists a source. Only:
      * the commenter IS the source (username matches the source handle), or
      * >= 3 DIFFERENT users made explicit rights claims on this source's clips in 7 days."""
    import sources_manager
    from models import Source
    now = now or utcnow()
    if not post.source_id:
        return None
    src = session.get(Source, post.source_id)
    if src is None or src.status == "BLACKLISTED":
        return None
    handle = _norm_handle(src.handle)
    if handle and _norm_handle(comment.username) == handle:
        sources_manager.blacklist_source(session, src.id, f"source owner @{comment.username} complained in comments")
        return "owner"
    rows = (session.query(Comment.username, Comment.text).join(Post, Post.id == Comment.post_id)
            .filter(Post.source_id == src.id, Comment.commented_at >= now - BLACKLIST_WINDOW).all())
    claimants = {(u or "").lower() for u, t in rows if CLAIM_RE.search(t or "")}
    claimants.discard("")
    if len(claimants) >= BLACKLIST_MIN_CLAIMANTS:
        sources_manager.blacklist_source(session, src.id,
                                         f"{len(claimants)} different users made rights claims in 7 days")
        return "claims"
    return None


def decide(session, comment, now=None, rng=None, llm=None, client_factory=client_for) -> str:
    now = now or utcnow()
    rng = rng or random.Random()
    acc = session.get(OwnedChannels, comment.owned_channel_id)
    post = session.get(Post, comment.post_id)
    label = comment.classification
    if post.published_at and comment.commented_at and \
            comment.commented_at - post.published_at > guardrails.REPLY_MAX_COMMENT_AGE_AFTER_POST:
        comment.status = "SKIPPED"
        return "SKIPPED"
    if label == "risky":
        comment.status, comment.needs_attention = "RISKY", True
        raise_alert("RISKY_COMMENT", f"Risky comment on {acc.ig_username or acc.id} (post {post.id}): "
                    f"“{(comment.text or '')[:300]}”", severity="WARNING", account_id=acc.id, session=session)
        maybe_blacklist_source(session, comment, post, now)
        return "RISKY"
    if label == "spam_abuse":
        ok, why = guardrails.can_hide(session, acc, now)
        if not ok:
            comment.status = "IGNORED"
            return "IGNORED"
        system_state.assert_not_stopped(session)
        try:
            client_factory(acc).hide_comment(comment.ig_comment_id, True)
            comment.status = "HIDDEN"
            log_decision(session, "comment_hide", {"comment_id": comment.id}, "OK", True, comment.class_reason,
                         account_id=acc.id)
        except IGError as e:
            log.warning("hide failed: %s", e)
            comment.status = "IGNORED"
        return comment.status
    if rng.random() >= REPLY_PROB.get(label, 0):
        comment.status = "IGNORED"
        return "IGNORED"
    text, err = write_reply(session, comment, llm)
    if not text:
        comment.status = "IGNORED"
        log_decision(session, "comment_reply", {"comment_id": comment.id}, f"REJECTED: {err}", False,
                     account_id=acc.id)
        return "IGNORED"
    lo, hi = guardrails.REPLY_DELAY_MINUTES
    when = now + _dt.timedelta(minutes=rng.uniform(lo, hi))
    session.add(CommentReply(comment_id=comment.id, owned_channel_id=acc.id, text=text, scheduled_for=when,
                             status="SCHEDULED", created_at=now))
    comment.status = "REPLY_SCHEDULED"
    log_decision(session, "comment_reply", {"comment_id": comment.id, "text": text, "send_at": when.isoformat()},
                 "OK", True, account_id=acc.id)
    return "REPLY_SCHEDULED"


def process_new(session, now=None, rng=None, llm=None, client_factory=client_for, lock_timeout=120) -> dict:
    """Classify + decide every NEW comment (Qwen needs the GPU lock)."""
    new = session.query(Comment).filter_by(status="NEW").order_by(Comment.id).all()
    out = {}
    if not new:
        return out
    try:
        with gpu_job("qwen_comments", timeout=lock_timeout):
            for c in new:
                classify_comment(session, c, llm)
                res = decide(session, c, now, rng, llm, client_factory)
                out[res] = out.get(res, 0) + 1
                session.commit()
            (llm or get_llm()).unload()
    except GPUBusy:
        log.info("GPU busy — comment classification postponed")
    return out


def send_due(session, now=None, client_factory=client_for) -> int:
    now = now or utcnow()
    sent = 0
    due = (session.query(CommentReply).filter(CommentReply.status == "SCHEDULED",
                                              CommentReply.scheduled_for <= now)
           .order_by(CommentReply.scheduled_for).all())
    for r in due:
        if system_state.emergency_stopped(session):
            break
        comment = session.get(Comment, r.comment_id)
        acc = session.get(OwnedChannels, r.owned_channel_id)
        post = session.get(Post, comment.post_id)
        ok, why = guardrails.can_reply(session, acc, post, comment, now)
        vid = session.get(videos, post.video_id)
        problems = reply_problems(r.text, f"{vid.transcript or ''}\n{vid.caption or ''}", [])
        if not ok or problems:
            if "cap" in why:
                r.scheduled_for = now + _dt.timedelta(minutes=random.uniform(20, 40))
            else:
                r.status, r.block_reason = "BLOCKED", (why or "; ".join(problems))[:300]
            continue
        try:
            res = client_factory(acc).reply(comment.ig_comment_id, r.text)
            r.status, r.sent_at, r.ig_reply_id = "SENT", now, str(res.get("id"))
            comment.status = "REPLIED"
            sent += 1
        except IGError as e:
            r.status, r.block_reason = "FAILED", str(e)[:300]
    session.commit()
    return sent


def tick(session, now=None):
    now = now or utcnow()
    system_state.heartbeat("comment_responder", session)
    if system_state.emergency_stopped(session):
        return
    send_due(session, now)
    jobs.run_if_due("comment_poll", _dt.timedelta(minutes=10),
                    lambda: (fetch_new(session, now), process_new(session, now)), now, session)


def main():
    import db_migrate
    import power
    db_migrate.run(log=log.info)
    power.prevent_sleep()
    system_state.register_service("comment_responder")
    while True:
        s = Session()
        try:
            tick(s)
        except Exception as e:
            log.error("tick failed: %s\n%s", e, traceback.format_exc())
        finally:
            s.close()
        time.sleep(60)


if __name__ == "__main__":
    main()
