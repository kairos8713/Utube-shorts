"""
brain/core.py — propose → validate → apply/reject → log.

    results = run_decision(session, task="account_review", system=..., user=...,
                           allowed={"account_pace", "account_pause", "noop"},
                           ctx={"account_ids": [acc.id]}, account_id=acc.id)

Qwen's reply must be {"actions": [{"type": ..., ...}], "reason": "..."}.
For each action, in order:
  1. type is in the global whitelist and not FORBIDDEN
  2. type is allowed for THIS task (narrow scope)
  3. schema + guardrail validator passes
  4. applier runs (same DB transaction), Decision row records the outcome
If the LLM call fails, a Decision row records it and `fallback()` (the previous /
safe default behaviour) is returned instead.
"""
import json
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from brain.actions import ACTIONS, FORBIDDEN_ACTIONS
from llm_client import LLMError, get_llm
from log_setup import get_logger
from models import Decision
from timeutil import utcnow

log = get_logger("brain")

MAX_ACTIONS_PER_DECISION = 20


@dataclass
class ActionResult:
    action: dict
    ok: bool
    validation: str
    applied: bool = False


@dataclass
class DecisionOutcome:
    results: List[ActionResult] = field(default_factory=list)
    reason: str = ""
    llm_failed: bool = False
    fallback_used: bool = False
    fallback_value: object = None

    @property
    def applied(self) -> List[dict]:
        return [r.action for r in self.results if r.applied]


def log_decision(session, task: str, proposed, validation: str, applied: bool, reason: str = "",
                 input_summary: str = "", account_id: Optional[int] = None):
    session.add(Decision(task=task, owned_channel_id=account_id, input_summary=(input_summary or "")[:2000],
                         proposed_action=json.dumps(proposed, ensure_ascii=False, default=str)[:4000],
                         reason=(reason or "")[:2000], validation=validation[:1000], applied=applied,
                         created_at=utcnow()))


def _structure(obj) -> list:
    acts = obj.get("actions")
    if not isinstance(acts, list):
        return ["'actions' must be a list"]
    if len(acts) > MAX_ACTIONS_PER_DECISION:
        return [f"at most {MAX_ACTIONS_PER_DECISION} actions"]
    if any(not isinstance(a, dict) or not isinstance(a.get("type"), str) for a in acts):
        return ["each action must be an object with a string 'type'"]
    return []


def validate_action(session, action: dict, allowed: set, ctx: dict) -> List[str]:
    t = action.get("type")
    if t in FORBIDDEN_ACTIONS:
        return [f"action '{t}' is forbidden"]
    if t not in ACTIONS:
        return [f"action '{t}' is not whitelisted"]
    if t not in allowed:
        return [f"action '{t}' is out of scope for this task"]
    validator, _ = ACTIONS[t]
    return validator(session, action, ctx)


def apply_actions(session, task: str, actions: list, allowed: set, ctx: dict, reason: str = "",
                  input_summary: str = "", account_id: int = None) -> List[ActionResult]:
    out = []
    for a in actions:
        errs = validate_action(session, a, allowed, ctx)
        if errs:
            log_decision(session, task, a, "REJECTED: " + "; ".join(errs), False, reason, input_summary, account_id)
            log.warning("[%s] rejected %s: %s", task, a.get("type"), errs)
            out.append(ActionResult(a, False, "; ".join(errs)))
            continue
        ACTIONS[a["type"]][1](session, a, ctx)
        log_decision(session, task, a, "OK", True, reason, input_summary, account_id)
        log.info("[%s] applied %s", task, a.get("type"))
        out.append(ActionResult(a, True, "OK", True))
    session.commit()
    return out


def run_decision(session, task: str, system: str, user: str, allowed: set, ctx: dict = None,
                 fallback: Callable = None, input_summary: str = "", account_id: int = None,
                 llm=None, max_tokens: int = 800) -> DecisionOutcome:
    ctx = ctx if ctx is not None else {}
    llm = llm or get_llm()
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        obj = llm.chat_json(msgs, task=task, validator=_structure, max_tokens=max_tokens)
    except LLMError as e:
        log_decision(session, task, None, f"LLM_FAILED: {e}", False, "", input_summary, account_id)
        session.commit()
        outcome = DecisionOutcome(llm_failed=True)
        if fallback:
            outcome.fallback_used = True
            outcome.fallback_value = fallback()
        return outcome
    reason = str(obj.get("reason") or "")[:2000]
    results = apply_actions(session, task, obj["actions"], allowed, ctx, reason, input_summary, account_id)
    return DecisionOutcome(results=results, reason=reason)
