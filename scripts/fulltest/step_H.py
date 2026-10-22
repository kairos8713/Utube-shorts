"""Step H: Qwen cycle report (real model) + panel on the copy DB: login with a throw-away password (only in the
panel process env), fetch every page, optional Playwright screenshots."""
import datetime as dt
import json
import os
import re
import secrets
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

RUN = common.activate()
common.set_step("H")

import models  # noqa: E402
import reports  # noqa: E402
import requests  # noqa: E402
from models import CycleReport, Post  # noqa: E402

PORT = 5071
PAGES = ["/", "/accounts", "/today", "/sources", "/sources?status=TRIAL", "/sources?status=BLACKLISTED", "/reports",
         "/decisions", "/decisions?applied=0", "/decisions?task=comment_reply", "/comments", "/guardrails"]
SHOTS = [("/", "home"), ("/accounts", "accounts"), ("/today", "today"), ("/reports", "reports"),
         ("/decisions", "decisions")]
GLOBAL_PY = r"C:\Users\Emre\AppData\Local\Programs\Python\Python311\python.exe"


def main():
    client = common.instrumented_llm()
    s = models.Session()
    posts = s.query(Post).filter_by(status="PUBLISHED").order_by(Post.published_at).all()
    since = min(p.created_at for p in posts) - dt.timedelta(days=1)
    until = max(p.published_at for p in posts) + dt.timedelta(days=4)
    t = time.time()
    rep = reports.build_cycle_report(s, 999, since, until, extra={"note": "full test"}, llm=client)
    report = {"id": rep.id, "seconds": round(time.time() - t, 1), "narrative": rep.narrative,
              "decision_summary": rep.decision_summary, "stats": json.loads(rep.stats_json)}

    password = secrets.token_urlsafe(16)
    from werkzeug.security import generate_password_hash
    env = dict(os.environ, PANEL_PASSWORD_HASH=generate_password_hash(password), PANEL_USERNAME="emre",
               PANEL_PORT=str(PORT), PANEL_HOST="127.0.0.1", PANEL_SECRET_KEY=secrets.token_hex(16))
    log = open(os.path.join(RUN, "logs", "panel_stdout.log"), "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, os.path.join(common.ROOT, "scripts", "fulltest", "run_service.py"),
                             "panel.py"], env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{PORT}"
    pages, shots = [], {}
    try:
        for _ in range(60):
            try:
                if requests.get(base + "/login", timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                time.sleep(1)
        sess = requests.Session()
        html = sess.get(base + "/login").text
        token = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
        r = sess.post(base + "/login", data={"username": "emre", "password": password, "_csrf": token},
                      allow_redirects=False)
        login_ok = r.status_code == 302
        for path in PAGES:
            t = time.time()
            r = sess.get(base + path)
            bad = any(x in r.text for x in ("Traceback", "Internal Server Error", "jinja2.exceptions"))
            pages.append({"path": path, "status": r.status_code, "ms": round((time.time() - t) * 1000),
                          "bytes": len(r.content), "error_text": bad})
        # Playwright (installed in the global Python) — optional
        shot_script = os.path.join(RUN, "work", "shots.py")
        with open(shot_script, "w", encoding="utf-8") as f:
            f.write(f"""
import sys
from playwright.sync_api import sync_playwright
base, pw, out = {base!r}, {password!r}, {os.path.join(RUN, 'screenshots')!r}
import os; os.makedirs(out, exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={{"width": 1366, "height": 900}})
    pg.goto(base + "/login"); pg.fill("input[name=username]", "emre"); pg.fill("input[name=password]", pw)
    pg.click("button[type=submit]"); pg.wait_for_load_state()
    for path, name in {SHOTS!r}:
        pg.goto(base + path); pg.wait_for_load_state(); pg.screenshot(path=os.path.join(out, name + ".png"), full_page=True)
    b.close()
print("ok")
""")
        r = subprocess.run([GLOBAL_PY, shot_script], capture_output=True, text=True, timeout=180)
        shots = {"exit": r.returncode, "stdout": r.stdout[-300:], "stderr": r.stderr[-800:],
                 "files": sorted(os.listdir(os.path.join(RUN, "screenshots"))) if os.path.isdir(
                     os.path.join(RUN, "screenshots")) else []}
        os.remove(shot_script)                      # contains the throw-away password
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        log.close()
    result = {"report": report, "login_ok": login_ok, "pages": pages,
              "all_200": all(p["status"] == 200 and not p["error_text"] for p in pages), "screenshots": shots,
              "qwen": common.qwen_stats("H"), "resources": common.sample_resources("H end")}
    common.save_result("H", result)
    print(json.dumps({k: result[k] for k in ("login_ok", "pages", "all_200", "screenshots", "qwen")}, indent=1))
    print("NARRATIVE:", report["narrative"])


if __name__ == "__main__":
    main()
