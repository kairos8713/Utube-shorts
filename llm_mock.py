"""
llm_mock.py — deterministic offline stand-in for Qwen (LLM_BACKEND=mock).

Used by the test-suite and by the end-to-end DRY_RUN when Ollama is not
installed. Each task returns schema-valid JSON so the surrounding code paths
(validation, guardrails, persistence) are exercised exactly as with the real
model. Tests can override a task with `mock.responses[task] = callable|dict`.
"""
import hashlib
import json
import re

from llm_client import _log_call


def _user_text(messages) -> str:
    return "\n".join(m["content"] for m in messages if m["role"] == "user")


def _block(text: str, label: str) -> str:
    m = re.search(rf"<<<DATA {label}\n(.*?)\n>>>END {label}", text, re.S)
    return m.group(1) if m else ""


def _seed(text: str) -> int:
    return int(hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8], 16)


def _clip_scoring(messages):
    tr = _block(_user_text(messages), "TRANSCRIPT")
    stamps = re.findall(r"\[(\d+):(\d+) - (\d+):(\d+)\]", tr)
    start = int(stamps[0][0]) * 60 + int(stamps[0][1]) if stamps else 5
    clips = [{"start": f"{start // 60:02d}:{start % 60:02d}",
              "end": f"{(start + 22) // 60:02d}:{(start + 22) % 60:02d}",
              "score": 80, "topic": "mock topic", "reason": "mock", "time_sensitive": False}]
    return {"clips": clips}


_COMMENTS = [
    "This is the part nobody talks about, and it changes how you see the whole story.",
    "Wild how one small decision here ended up mattering more than everything else.",
    "Hard to argue with this once you hear the numbers behind it.",
    "The calm way this gets explained makes it even more surprising.",
    "Would you have made the same call in that moment?",
]


def _caption(messages):
    text = _user_text(messages)
    s = _seed(text)
    return {"comment": _COMMENTS[s % len(_COMMENTS)],
            "hashtags": ["#podcastclips", "#mindset", "#storytime"]}


def _comment_classify(messages):
    c = _block(_user_text(messages), "COMMENT").lower()
    if any(w in c for w in ("copyright", "my video", "stolen", "take down", "takedown", "remove this", "dmca")):
        label = "risky"
    elif any(w in c for w in ("http", "follow me", "check my", "idiot", "stupid", "f*ck", "fuck")):
        label = "spam_abuse"
    elif "?" in c:
        label = "question"
    elif len(re.sub(r"\W", "", c)) <= 6:
        label = "emoji_short"
    else:
        label = "discussion"
    return {"label": label, "reason": "mock"}


def _comment_reply(messages):
    s = _seed(_user_text(messages))
    replies = ["Exactly, that moment says a lot.", "Good question — the clip hints at it but doesn't say.",
               "Same reaction here, it hits differently the second time.",
               "Fair point, there's more context in the full conversation."]
    return {"reply": replies[s % len(replies)]}


def _slot_plan(messages):
    data = _block(_user_text(messages), "SLOT_TABLE")
    try:
        table = json.loads(data)
    except ValueError:
        table = {}
    actions = []
    for acc in table.get("accounts", []):
        ranked = sorted((r for r in acc.get("slots", []) if r.get("n", 0) >= 4),
                        key=lambda r: -(r.get("relative") or 0))
        slots = [r["slot"] for r in ranked][:2] or [19, 21]
        actions.append({"type": "slot_assign", "account_id": acc["account_id"], "slots": slots})
    return {"actions": actions, "reason": "mock: highest relative performance slots"}


def _clip_ranking(messages):
    ids = re.findall(r"^\[(c\d+)\]", _block(_user_text(messages), "CANDIDATES"), re.M)
    return {"ranking": [{"id": i, "score": 90 - 7 * k} for k, i in enumerate(ids)]}


def _source_queries(messages):
    niche = re.search(r"NICHE: (.+)", _user_text(messages))
    n = (niche.group(1).strip() if niche else "podcast")
    return {"queries": [f"{n} podcast full episode", f"{n} interview long form"]}


def _source_assessment(messages):
    return {"niche_fit": 0.7, "format": "ok", "clip_permission": "unknown", "clip_permission_evidence": "",
            "reason": "mock assessment"}


def _account_review(messages):
    return {"actions": [{"type": "noop"}], "reason": "mock: not enough evidence to pause"}


def _report_narrative(messages):
    return {"narrative": "Mock cycle summary: production and publishing ran in DRY_RUN mode.",
            "decision_summary": "No risky decisions were taken."}


TASKS = {
    "clip_scoring": _clip_scoring,
    "caption": _caption,
    "comment_classify": _comment_classify,
    "comment_reply": _comment_reply,
    "slot_plan": _slot_plan,
    "clip_ranking": _clip_ranking,
    "source_queries": _source_queries,
    "source_assessment": _source_assessment,
    "account_review": _account_review,
    "report_narrative": _report_narrative,
}


class MockLLM:
    backend = "mock"

    def __init__(self):
        self.responses = {}
        self.calls = []

    def health(self) -> bool:
        return True

    def unload(self):
        pass

    def chat_json(self, messages, task, validator=None, **kw):
        self.calls.append({"task": task, "messages": messages})
        override = self.responses.get(task)
        if callable(override):
            obj = override(messages)
        elif override is not None:
            obj = json.loads(json.dumps(override))
        elif task in TASKS:
            obj = TASKS[task](messages)
        else:
            raise KeyError(f"MockLLM has no handler for task {task!r}")
        _log_call(task, 0.0, None, None, "mock")
        if validator:
            errs = validator(obj)
            if errs:
                from llm_client import LLMBadOutput
                raise LLMBadOutput(f"{task}: mock output invalid: {errs}")
        return obj

    def chat(self, messages, task, **kw):
        return json.dumps(self.chat_json(messages, task))
