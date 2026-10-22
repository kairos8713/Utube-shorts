"""
Full-system test harness (docs/FULL_TEST_REPORT.md). Import `activate()` BEFORE any project module.

* every path (DB copy, work/reels/downloads/segments/logs/backups) lives under test_runs/full_<ts>/
* DRY_RUN forced on, e-mail → outbox, R2/SMTP credentials blanked for the process
* network guard: requests to Instagram / Meta upload / R2 and any SMTP connection raise and are
  recorded in <run>/network_violations.jsonl (must stay empty)
* instrumented Ollama client: per-call duration, token/s, raw think leakage, retries → qwen_calls.jsonl
* resource sampler: nvidia-smi VRAM per GPU + disk → resources.jsonl
"""
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CURRENT = os.path.join(ROOT, "test_runs", "CURRENT.txt")
NVIDIA_SMI = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
BLOCKED_HOSTS = ("graph.instagram.com", "api.instagram.com", "www.instagram.com", "rupload.facebook.com",
                 "graph.facebook.com", "r2.cloudflarestorage.com", "r2.dev")

RUN = None


def run_dir() -> str:
    with open(CURRENT, encoding="utf-8") as f:
        return f.read().strip()


def env_for(run: str) -> dict:
    return {
        "DATABASE_URL": "sqlite:///" + os.path.join(run, "automation_copy.db").replace("\\", "/"),
        "WORK_DIR": os.path.join(run, "work"), "REELS_DIR": os.path.join(run, "reels"),
        "DOWNLOAD_DIR": os.path.join(run, "downloads"), "SEGMENTS_DIR": os.path.join(run, "segments"),
        "LOG_DIR": os.path.join(run, "logs"), "BACKUP_DIR": os.path.join(run, "backups"),
        "DRY_RUN": "true", "EMAIL_ENABLED": "true", "EMAIL_TEST_MODE": "true",
        "SMTP_PASSWORD": "", "SMTP_HOST": "smtp.invalid", "R2_ENDPOINT_URL": "", "R2_ACCESS_KEY_ID": "",
        "R2_SECRET_ACCESS_KEY": "", "LLM_BACKEND": "ollama", "PYTHONIOENCODING": "utf-8",
    }


def activate(run: str = None, guard: bool = True) -> str:
    global RUN
    RUN = run or run_dir()
    os.environ.update(env_for(RUN))
    for d in ("work", "reels", "downloads", "segments", "logs", "backups", "results", "frames"):
        os.makedirs(os.path.join(RUN, d), exist_ok=True)
    sys.path.insert(0, ROOT)
    if guard:
        install_network_guard()
    return RUN


# ── network guard ───────────────────────────────────────────────────────────

def _violation(kind, target):
    rec = {"ts": time.time(), "kind": kind, "target": str(target)[:300], "pid": os.getpid()}
    with open(os.path.join(RUN, "network_violations.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    raise RuntimeError(f"NETWORK GUARD: blocked {kind} to {target}")


def install_network_guard():
    import smtplib

    import requests.sessions
    orig = requests.sessions.Session.request

    def guarded(self, method, url, *a, **k):
        if any(h in str(url) for h in BLOCKED_HOSTS):
            _violation("http", f"{method} {url}")
        return orig(self, method, url, *a, **k)
    requests.sessions.Session.request = guarded

    class GuardSMTP:
        def __init__(self, *a, **k):
            _violation("smtp", a[:1])
    smtplib.SMTP = GuardSMTP
    smtplib.SMTP_SSL = GuardSMTP
    try:
        import boto3

        def no_boto(*a, **k):
            _violation("boto3", a[:1])
        boto3.client = no_boto
    except ImportError:
        pass


# ── instrumented Ollama client ──────────────────────────────────────────────

STEP = {"name": "setup"}


def instrumented_llm(model: str = None):
    """OllamaClient whose transport records raw per-call metrics; installed via llm_client.set_llm."""
    import llm_client
    from config import settings

    def transport(method, url, payload, timeout):
        t0 = time.time()
        status, body = llm_client._requests_transport(method, url, payload, timeout)
        if url.endswith("/api/chat"):
            msg = (body or {}).get("message") or {}
            content = msg.get("content") or ""
            last_user = next((m["content"] for m in reversed(payload.get("messages", [])) if m["role"] == "user"), "")
            ev, evd = body.get("eval_count") or 0, body.get("eval_duration") or 0
            pe, ped = body.get("prompt_eval_count") or 0, body.get("prompt_eval_duration") or 0
            num_ctx = payload.get("options", {}).get("num_ctx") or settings.OLLAMA_NUM_CTX
            num_predict = payload.get("options", {}).get("num_predict") or 0
            rec = {"ts": t0, "step": STEP["name"], "model": payload.get("model"), "status": status,
                   "seconds": round(time.time() - t0, 2), "json_mode": payload.get("format") == "json",
                   "think_param": payload.get("think"), "prompt_tokens": pe, "completion_tokens": ev,
                   "gen_tok_s": round(ev / (evd / 1e9), 2) if evd else None,
                   "prompt_tok_s": round(pe / (ped / 1e9), 2) if ped else None,
                   "load_s": round((body.get("load_duration") or 0) / 1e9, 2),
                   "think_tag_in_content": "<think>" in content,
                   "thinking_field_chars": len(msg.get("thinking") or ""),
                   "content_chars": len(content), "is_retry": "was rejected" in last_user,
                   "near_ctx_limit": bool(pe and pe + num_predict >= num_ctx),
                   "num_ctx": num_ctx, "content_preview": content[:300]}
            with open(os.path.join(RUN, "qwen_calls.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return status, body

    client = llm_client.OllamaClient(model=model, transport=transport)
    llm_client.set_llm(client)
    return client


def set_step(name: str):
    STEP["name"] = name


# ── resources / results ─────────────────────────────────────────────────────

def sample_resources(label: str) -> dict:
    rec = {"ts": time.time(), "label": label}
    try:
        out = subprocess.run([NVIDIA_SMI, "--query-gpu=index,memory.used,memory.total,utilization.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=20).stdout
        rec["gpus"] = [dict(zip(("index", "mem_used_mb", "mem_total_mb", "util_pct"),
                                [int(x.strip()) for x in line.split(",")])) for line in out.strip().splitlines()]
    except Exception as e:
        rec["gpus_error"] = str(e)
    du = shutil.disk_usage(ROOT)
    rec["disk_free_gb"] = round(du.free / 1e9, 1)
    rec["run_dir_mb"] = round(sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(RUN)
                                  for f in fs if os.path.exists(os.path.join(d, f))) / 1e6, 1)
    with open(os.path.join(RUN, "resources.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def save_result(step: str, data: dict):
    path = os.path.join(RUN, "results", f"{step}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    return path


def load_result(step: str) -> dict:
    with open(os.path.join(RUN, "results", f"{step}.json"), encoding="utf-8") as f:
        return json.load(f)


def qwen_stats(step: str = None) -> dict:
    path = os.path.join(RUN, "qwen_calls.jsonl")
    if not os.path.exists(path):
        return {"calls": 0}
    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    if step:
        rows = [r for r in rows if r["step"] == step]
    if not rows:
        return {"calls": 0}
    secs = [r["seconds"] for r in rows]
    gen = [r["gen_tok_s"] for r in rows if r["gen_tok_s"]]
    return {"calls": len(rows), "avg_s": round(sum(secs) / len(secs), 2), "max_s": max(secs),
            "avg_gen_tok_s": round(sum(gen) / len(gen), 2) if gen else None,
            "retries": sum(r["is_retry"] for r in rows),
            "think_leaks": sum(r["think_tag_in_content"] or r["thinking_field_chars"] > 0 for r in rows),
            "near_ctx_limit": sum(r["near_ctx_limit"] for r in rows),
            "max_prompt_tokens": max(r["prompt_tokens"] for r in rows)}


class Budget:
    """At most N YouTube video/audio downloads for the whole test (task rule: 8)."""

    def __init__(self, limit: int = 8):
        self.path = os.path.join(RUN, "download_budget.json")
        self.limit = limit

    def used(self) -> list:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def take(self, what: str) -> bool:
        used = self.used()
        if len(used) >= self.limit:
            return False
        used.append({"ts": time.time(), "what": what})
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(used, f, indent=2)
        return True
