"""
watchdog.py — keeps the other services alive (separate process).

Every minute:
  * heartbeat of cycle_runner / publisher / comment_responder too old →
    kill the stuck PID (if alive) and start the service again
  * panel not answering HTTP → restart panel.py
  * Ollama not answering (LLM_BACKEND=ollama) → try `ollama serve`
  * inside the posting window, queue items were due but nothing was published
    for NO_UPLOAD_HOURS → WATCHDOG alert + e-mail
  * max 3 restarts per service per hour; a failed restart → e-mail
Writes its own heartbeat ("watchdog").
"""
import datetime as _dt
import json
import os
import subprocess
import sys
import time
import traceback

import system_state
from alerts import raise_alert
from config import ROOT_DIR, settings
from log_setup import get_logger
from models import Post, Session, UploadQueue
from timeutil import utcnow, window_day_of

log = get_logger("watchdog")

SERVICES = {
    "cycle_runner": {"script": "cycle_runner.py", "stale_min": 20},
    "publisher": {"script": "publish_runner.py", "stale_min": 5},
    "comment_responder": {"script": "comment_responder.py", "stale_min": 5},
}
NO_UPLOAD_HOURS = 6
MAX_RESTARTS_PER_HOUR = 3


def _pid_alive(pid) -> bool:
    try:
        import psutil
        return bool(pid) and psutil.pid_exists(pid)
    except ImportError:
        return False


def default_restarter(script: str, pid=None) -> bool:
    try:
        if pid and _pid_alive(pid):
            import psutil
            psutil.Process(pid).kill()
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([sys.executable, os.path.join(ROOT_DIR, script)], cwd=ROOT_DIR, creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        log.error("restart of %s failed: %s", script, e)
        return False


def _restart_allowed(session, name, now) -> bool:
    key = f"restarts:{name}"
    try:
        hist = [_dt.datetime.fromisoformat(x) for x in json.loads(system_state.get(key, "[]", session))]
    except ValueError:
        hist = []
    hist = [h for h in hist if now - h < _dt.timedelta(hours=1)]
    if len(hist) >= MAX_RESTARTS_PER_HOUR:
        return False
    hist.append(now)
    system_state.put(key, json.dumps([h.isoformat() for h in hist]), session)
    return True


def _restart(session, name, script, now, restarter, pid=None) -> str:
    if not _restart_allowed(session, name, now):
        raise_alert("SERVICE_RESTART_FAILED", f"{name}: restart limit ({MAX_RESTARTS_PER_HOUR}/h) reached — "
                    "needs a human", severity="CRITICAL", session=session)
        return "limit"
    if restarter(script, pid):
        log.warning("%s restarted", name)
        return "restarted"
    raise_alert("SERVICE_RESTART_FAILED", f"{name}: could not be restarted", severity="CRITICAL", session=session)
    return "failed"


def check_services(session, now=None, restarter=default_restarter) -> dict:
    now = now or utcnow()
    out = {}
    for name, cfg in SERVICES.items():
        hb = system_state.last_heartbeat(name, session)
        if hb is None:
            out[name] = "never_started"
            continue
        if now - hb <= _dt.timedelta(minutes=cfg["stale_min"]):
            out[name] = "ok"
            continue
        out[name] = _restart(session, name, cfg["script"], now, restarter, system_state.service_pid(name, session))
    return out


def check_panel(session, now=None, http_get=None, restarter=default_restarter) -> str:
    import requests
    http_get = http_get or (lambda url: requests.get(url, timeout=5).status_code)
    try:
        if http_get(f"http://{settings.PANEL_HOST}:{settings.PANEL_PORT}/login") < 500:
            return "ok"
    except Exception:
        pass
    return _restart(session, "panel", "panel.py", now or utcnow(), restarter)


def check_ollama(session, now=None, health=None, starter=None) -> str:
    if settings.LLM_BACKEND != "ollama":
        return "skipped"
    from llm_client import OllamaClient
    health = health or (lambda: OllamaClient(timeout=10).health())
    if health():
        return "ok"
    wait_s = 0 if starter else 15          # injected starters (tests) don't need the boot wait
    from llm_client import ollama_exe
    starter = starter or (lambda: subprocess.Popen([ollama_exe(), "serve"], stdout=subprocess.DEVNULL,
                                                   stderr=subprocess.DEVNULL))
    try:
        starter()
        time.sleep(wait_s)
        if health():
            return "restarted"
    except Exception as e:
        log.error("ollama start failed: %s", e)
    raise_alert("SERVICE_RESTART_FAILED", f"Ollama ({settings.OLLAMA_URL}) not responding and could not be started",
                severity="CRITICAL", session=session)
    return "failed"


def check_publishing(session, now=None) -> str:
    now = now or utcnow()
    if system_state.emergency_stopped(session) or window_day_of(now) is None:
        return "n/a"
    since = now - _dt.timedelta(hours=NO_UPLOAD_HOURS)
    due = (session.query(UploadQueue).filter(UploadQueue.status.in_(("PENDING", "UPLOADED", "FAILED")),
                                             UploadQueue.scheduled_for >= since,
                                             UploadQueue.scheduled_for <= now - _dt.timedelta(minutes=30)).count())
    if not due:
        return "nothing_due"
    ok = session.query(Post).filter(Post.status == "PUBLISHED", Post.published_at >= since).count()
    if ok:
        return "ok"
    raise_alert("WATCHDOG", f"{due} posts were due in the last {NO_UPLOAD_HOURS} h inside the posting window "
                "but none was published", severity="CRITICAL", session=session)
    return "alert"


def tick(session, now=None, **kw) -> dict:
    now = now or utcnow()
    system_state.heartbeat("watchdog", session)
    return {"services": check_services(session, now, kw.get("restarter", default_restarter)),
            "panel": check_panel(session, now, kw.get("http_get"), kw.get("restarter", default_restarter)),
            "ollama": check_ollama(session, now, kw.get("health"), kw.get("starter")),
            "publishing": check_publishing(session, now)}


def main():
    import power
    power.prevent_sleep()
    system_state.register_service("watchdog")
    while True:
        s = Session()
        try:
            tick(s)
        except Exception as e:
            log.error("watchdog tick failed: %s\n%s", e, traceback.format_exc())
        finally:
            s.close()
        time.sleep(60)


if __name__ == "__main__":
    main()
