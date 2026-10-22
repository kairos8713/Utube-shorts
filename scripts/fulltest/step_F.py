"""Step F: 30 realistic comments on the DRY_RUN-published posts; real Qwen classification + replies."""
import datetime as dt
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

RUN = common.activate()
if os.environ.get("FT_DB"):          # re-run on a scratch DB (decisions re-run: F2)
    os.environ["DATABASE_URL"] = "sqlite:///" + os.environ["FT_DB"].replace("\\", "/")
RESULT = os.environ.get("FT_RESULT", "F")
common.set_step(RESULT)

import comment_responder as cr  # noqa: E402
import models  # noqa: E402
from comments import COMMENTS, SOURCE_OWNER  # noqa: E402
from instagram.client import client_for  # noqa: E402
from models import Alert, Comment, CommentReply, Decision, OwnedChannels, Post, Source  # noqa: E402


def main():
    client = common.instrumented_llm()
    s = models.Session()
    posts = s.query(Post).filter_by(status="PUBLISHED").order_by(Post.published_at).all()
    by_source = {}
    for p in posts:
        by_source.setdefault(p.source_id, []).append(p)
    sources = [s.get(Source, sid) for sid in by_source if sid]
    owner_src = next((x for x in sources if x.handle), sources[0])
    other_src = next((x for x in sources if x.id != owner_src.id), owner_src)
    rng = random.Random(42)
    placed = []
    for n, (kind, user, text) in enumerate(COMMENTS):
        if kind == "claim_owner":
            p = by_source[owner_src.id][0]
            user = owner_src.handle or "unknown_owner"
        elif kind == "claim":
            p = by_source[other_src.id][len(placed) % len(by_source[other_src.id])]
        else:
            p = posts[n % len(posts)]
        c = Comment(ig_comment_id=f"F{n}", post_id=p.id, owned_channel_id=p.owned_channel_id, username=user,
                    text=text.replace(SOURCE_OWNER, user), commented_at=p.published_at + dt.timedelta(minutes=30 + n),
                    status="NEW")
        s.add(c)
        s.flush()
        placed.append({"comment_id": c.id, "kind": kind, "user": user, "text": c.text, "post": p.id,
                       "account": p.owned_channel_id, "source_id": p.source_id})
    s.commit()
    now = max(p.published_at for p in posts) + dt.timedelta(hours=1)
    t0 = time.time()
    summary = cr.process_new(s, now, rng, llm=client, client_factory=client_for)
    classify_seconds = round(time.time() - t0, 1)
    sent = []
    for minutes in (75, 150, 240, 360):
        sent.append({"at_min": minutes, "sent": cr.send_due(s, now + dt.timedelta(minutes=minutes),
                                                           client_factory=client_for)})
    rows = []
    for meta in placed:
        c = s.get(Comment, meta["comment_id"])
        r = s.query(CommentReply).filter_by(comment_id=c.id).first()
        rej = s.query(Decision).filter(Decision.task == "comment_reply",
                                       Decision.proposed_action.like(f'%"comment_id": {c.id}%'),
                                       Decision.validation != "OK").first()
        rows.append({**meta, "label": c.classification, "class_reason": c.class_reason, "status": c.status,
                     "needs_attention": c.needs_attention, "reply": r.text if r else None,
                     "reply_status": r.status if r else None, "block_reason": r.block_reason if r else None,
                     "delay_min": round((r.scheduled_for - now).total_seconds() / 60, 1) if r else None,
                     "reply_rejected": rej.validation if rej else None})
    injection = [x for x in rows if x["kind"] == "injection"]
    inj_check = {"replies_with_link_or_mention": [x["reply"] for x in injection if x["reply"] and
                                                  (cr.LINK_RE.search(x["reply"]) or cr.MENTION_RE.search(x["reply"]))],
                 "non_comment_decisions": [(d.task, d.validation) for d in s.query(Decision).filter(
                     ~Decision.task.in_(["comment_classify", "comment_reply", "comment_hide"]),
                     Decision.created_at >= dt.datetime.utcnow() - dt.timedelta(hours=2)).all()],
                 "accounts_paused": [a.id for a in s.query(OwnedChannels).filter(OwnedChannels.status != "ACTIVE",
                                                                                  OwnedChannels.name.like("test_%"))]}
    delays = [x["delay_min"] for x in rows if x["delay_min"] is not None]
    result = {"summary": summary, "classify_and_reply_seconds": classify_seconds, "sent": sent, "rows": rows,
              "label_matrix": {k: [x["label"] for x in rows if x["kind"] == k] for k in dict.fromkeys(x["kind"] for x in rows)},
              "replied_share": round(sum(1 for x in rows if x["reply_status"] == "SENT") / len(rows), 2),
              "delays_in_40_70": all(40 <= d <= 70 for d in delays), "delay_range": [min(delays), max(delays)] if delays else None,
              "injection_check": inj_check,
              "blacklist": {"owner_source": [owner_src.title, owner_src.handle, s.get(Source, owner_src.id).status,
                                             s.get(Source, owner_src.id).status_reason],
                            "other_source": [other_src.title, s.get(Source, other_src.id).status]},
              "alerts": [(a.kind, a.message[:120]) for a in s.query(Alert).filter(Alert.kind == "RISKY_COMMENT")],
              "qwen": common.qwen_stats(RESULT), "resources": common.sample_resources("F end")}
    common.save_result(RESULT, result)
    print(json.dumps({k: result[k] for k in ("summary", "classify_and_reply_seconds", "sent", "label_matrix",
                                             "replied_share", "delays_in_40_70", "delay_range", "injection_check",
                                             "blacklist", "qwen")}, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
