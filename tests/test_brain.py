import json

from brain import ACTIONS, FORBIDDEN_ACTIONS, run_decision
from helpers import account, source
from llm_client import LLMUnavailable
from llm_mock import MockLLM
from models import Decision, OwnedChannels, Source


def _run(db, actions, allowed, ctx=None, account_id=None):
    llm = MockLLM()
    llm.responses["t"] = {"actions": actions, "reason": "because"}
    return run_decision(db, "t", "sys", "user", allowed, ctx=ctx or {}, llm=llm, account_id=account_id)


def test_forbidden_actions_have_no_implementation():
    assert not FORBIDDEN_ACTIONS & set(ACTIONS)
    for name in ("account_resume", "delete_post", "change_password", "revoke_token", "change_guardrail"):
        assert name in FORBIDDEN_ACTIONS


def test_forbidden_unknown_and_out_of_scope_are_rejected(db):
    a = account(db, "a")
    out = _run(db, [{"type": "change_password", "account_id": a.id},
                    {"type": "launch_rocket"},
                    {"type": "account_pause", "account_id": a.id, "reason": "too risky now"}],
               allowed={"account_pace"}, ctx={"account_ids": [a.id]})
    v = [r.validation for r in out.results]
    assert "forbidden" in v[0] and "not whitelisted" in v[1] and "out of scope" in v[2]
    assert not out.applied
    assert db.get(OwnedChannels, a.id).status == "ACTIVE"
    rows = db.query(Decision).all()
    assert len(rows) == 3 and all(r.validation.startswith("REJECTED") for r in rows)


def test_excessive_pace_rejected_and_pause_applied(db):
    a = account(db, "young", active_days=3)
    out = _run(db, [{"type": "account_pace", "account_id": a.id, "daily_post_cap": 2},
                    {"type": "account_pause", "account_id": a.id, "reason": "reach collapsed 80%"}],
               allowed={"account_pace", "account_pause"}, ctx={"account_ids": [a.id]})
    assert not out.results[0].ok and out.results[1].applied
    a = db.get(OwnedChannels, a.id)
    assert a.status == "PAUSED_AUTO" and a.daily_post_cap == 1 and "qwen" in a.pause_reason


def test_account_scope_is_narrow(db):
    a, b = account(db, "a"), account(db, "b")
    out = _run(db, [{"type": "account_pause", "account_id": b.id, "reason": "not my account"}],
               allowed={"account_pause"}, ctx={"account_ids": [a.id]})
    assert "outside" in out.results[0].validation


def test_blacklisted_source_cannot_be_revived(db):
    s = source(db, status="BLACKLISTED")
    out = _run(db, [{"type": "source_trial", "source_id": s.id}], allowed={"source_trial"})
    assert not out.results[0].ok and db.get(Source, s.id).status == "BLACKLISTED"


def test_slot_assign_validates_keys(db):
    a = account(db, "a")
    out = _run(db, [{"type": "slot_assign", "account_id": a.id, "slots": [19, 4]}],
               allowed={"slot_assign"}, ctx={"account_ids": [a.id], "valid_slots": [15, 17, 19, 21, 23, 1, 3]})
    assert not out.results[0].ok and "invalid slot" in out.results[0].validation


def test_llm_failure_uses_fallback_and_is_logged(db):
    class Down(MockLLM):
        def chat_json(self, *a, **k):
            raise LLMUnavailable("down")
    out = run_decision(db, "t2", "s", "u", {"noop"}, llm=Down(), fallback=lambda: "previous-plan")
    assert out.llm_failed and out.fallback_value == "previous-plan"
    assert db.query(Decision).filter_by(task="t2").one().validation.startswith("LLM_FAILED")
