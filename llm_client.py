"""
llm_client.py — Ollama HTTP client for the local Qwen3-32B.

* JSON tasks: `format: "json"` + `think: false` (+ "/no_think" and <think> stripping
  as a fallback), parse + validate, up to 2 corrective retries.
* Network: timeout, retry with backoff on connection errors; if the service is
  down → LLMUnavailable with a clear message.
* VRAM: the model stays loaded during a batch (keep_alive OLLAMA_KEEP_ALIVE);
  call unload() at the end of the batch (keep_alive 0).
* Every call is logged (task, seconds, prompt/completion tokens) to logs/llm.log
  and logs/llm_calls.jsonl.
"""
import json
import os
import re
import time
from typing import Callable, List, Optional

from config import settings
from log_setup import get_logger

log = get_logger("llm")

THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    pass


class LLMBadOutput(LLMError):
    pass


def _requests_transport(method: str, url: str, payload: Optional[dict], timeout: float):
    import requests
    try:
        resp = requests.request(method, url, json=payload, timeout=timeout)
    except requests.exceptions.ConnectionError as e:
        raise ConnectionError(str(e)) from e
    except requests.exceptions.Timeout as e:
        raise TimeoutError(str(e)) from e
    try:
        body = resp.json()
    except ValueError:
        body = {"error": resp.text[:500]}
    return resp.status_code, body


def extract_json(text: str):
    """Parse a JSON object from model output (tolerates <think> blocks and code fences)."""
    text = THINK_RE.sub("", text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


class OllamaClient:
    backend = "ollama"

    def __init__(self, base_url: str = None, model: str = None, timeout: float = None,
                 transport: Callable = None, sleep=time.sleep):
        self.base_url = (base_url or settings.OLLAMA_URL).rstrip("/")
        self.model = model or settings.OLLAMA_MODEL
        self.timeout = timeout or settings.OLLAMA_TIMEOUT_S
        self.transport = transport or _requests_transport
        self.sleep = sleep

    # ── low level ────────────────────────────────────────────────────────────
    def _call(self, path: str, payload: Optional[dict], method: str = "POST",
              attempts: int = 3, timeout: float = None):
        last = None
        for i in range(attempts):
            try:
                status, body = self.transport(method, self.base_url + path, payload,
                                              timeout or self.timeout)
            except (ConnectionError, TimeoutError, OSError) as e:
                last = e
                log.warning("ollama %s attempt %d/%d failed: %s", path, i + 1, attempts, e)
                self.sleep(min(2 ** i * 2, 20))
                continue
            if status >= 500:
                last = LLMError(f"HTTP {status}: {body}")
                self.sleep(min(2 ** i * 2, 20))
                continue
            if status >= 400:
                raise LLMError(f"Ollama HTTP {status}: {body.get('error', body)}")
            return body
        if isinstance(last, (ConnectionError, TimeoutError, OSError)):
            raise LLMUnavailable(
                f"Ollama is not reachable at {self.base_url} ({last}). "
                f"Is the Ollama service running and the model '{self.model}' created?")
        raise LLMError(str(last))

    def health(self) -> bool:
        try:
            body = self._call("/api/tags", None, method="GET", attempts=1, timeout=5)
        except LLMError:
            return False
        names = {m.get("name", "").split(":")[0] for m in body.get("models", [])}
        return self.model.split(":")[0] in names

    def unload(self):
        """Free VRAM now (keep_alive 0)."""
        try:
            self._call("/api/generate", {"model": self.model, "keep_alive": 0}, attempts=1, timeout=30)
            log.info("model %s unloaded", self.model)
        except LLMError as e:
            log.warning("unload failed: %s", e)

    # ── chat ─────────────────────────────────────────────────────────────────
    def chat(self, messages: List[dict], task: str, json_mode: bool = False,
             max_tokens: int = 1024, temperature: float = 0.6, keep_alive: str = None) -> str:
        msgs = [dict(m) for m in messages]
        if json_mode and msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"] = msgs[-1]["content"].rstrip() + "\n/no_think"
        payload = {
            "model": self.model,
            "messages": msgs,
            "stream": False,
            "keep_alive": keep_alive or settings.OLLAMA_KEEP_ALIVE,
            "options": {"num_ctx": settings.OLLAMA_NUM_CTX, "temperature": temperature,
                        "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
            payload["think"] = False
        t0 = time.time()
        body = self._call("/api/chat", payload)
        elapsed = time.time() - t0
        content = (body.get("message") or {}).get("content", "")
        _log_call(task, elapsed, body.get("prompt_eval_count"), body.get("eval_count"), self.backend)
        return THINK_RE.sub("", content).strip()

    def chat_json(self, messages: List[dict], task: str, validator: Callable = None,
                  max_tokens: int = 1024, temperature: float = 0.4, retries: int = 2) -> dict:
        """Returns a dict that passed `validator(obj) -> list[str] errors` (empty = ok)."""
        convo = list(messages)
        errors = []
        for attempt in range(retries + 1):
            raw = self.chat(convo, task=task, json_mode=True, max_tokens=max_tokens,
                            temperature=temperature)
            try:
                obj = extract_json(raw)
                if not isinstance(obj, dict):
                    raise ValueError("top-level JSON must be an object")
                errors = validator(obj) if validator else []
            except (ValueError, json.JSONDecodeError) as e:
                errors = [f"invalid JSON: {e}"]
            if not errors:
                return obj
            log.warning("task %s attempt %d invalid: %s", task, attempt + 1, errors)
            convo = list(messages) + [
                {"role": "assistant", "content": raw[:4000]},
                {"role": "user", "content": "Your previous reply was rejected: " + "; ".join(errors)[:800]
                 + ". Reply again with ONE corrected JSON object only."},
            ]
        raise LLMBadOutput(f"{task}: no valid JSON after {retries + 1} attempts: {errors}")


def _log_call(task, seconds, prompt_tokens, completion_tokens, backend):
    log.info("task=%s backend=%s %.1fs prompt_tokens=%s completion_tokens=%s",
             task, backend, seconds, prompt_tokens, completion_tokens)
    try:
        os.makedirs(settings.LOG_DIR, exist_ok=True)
        with open(os.path.join(settings.LOG_DIR, "llm_calls.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "task": task, "backend": backend,
                                "seconds": round(seconds, 2), "prompt_tokens": prompt_tokens,
                                "completion_tokens": completion_tokens}) + "\n")
    except OSError:
        pass


_client = None


def get_llm():
    """Process-wide client. LLM_BACKEND=mock → deterministic offline mock (tests / E2E)."""
    global _client
    if _client is None:
        if settings.LLM_BACKEND == "mock":
            from llm_mock import MockLLM
            _client = MockLLM()
        else:
            _client = OllamaClient()
    return _client


def set_llm(client):
    global _client
    _client = client
