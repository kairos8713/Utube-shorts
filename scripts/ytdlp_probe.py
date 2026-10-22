"""
ytdlp_probe.py — try one yt-dlp configuration against a few YouTube videos and record
exactly what happened (maintenance tool; see docs/YTDLP_FIX.md).

  .venv\\Scripts\\python.exe scripts\\ytdlp_probe.py --label a_ejs_node \\
        --clients default --js node --no-cookies URL1 URL2 URL3

Per URL it downloads a small rendition (<= 360p by default, --height to change) into a temp
folder, then deletes it. Results (exit code, format, size, PO-token debug lines, last 40
stderr lines) are appended as JSON lines to logs/ytdlp_fix/probe_results.jsonl.
Uses full paths for node/ffmpeg because PATH on this PC is incomplete.
"""
import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "logs", "ytdlp_fix")
NODE = r"C:\Program Files\nodejs\node.exe"
DENO_CANDIDATES = [os.path.join(ROOT, "tools", "deno", "deno.exe"),
                   os.path.join(os.path.expanduser("~"), ".deno", "bin", "deno.exe")]
FFMPEG_DIR = os.path.dirname(shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe")


def js_runtime_args(js: str) -> list:
    if js == "node":
        return ["--js-runtimes", f"node:{NODE}"]
    if js == "deno":
        deno = next((p for p in DENO_CANDIDATES if os.path.exists(p)), None)
        return ["--js-runtimes", f"deno:{deno}"] if deno else ["--js-runtimes", "deno"]
    return []                      # "default": let yt-dlp decide


def build_cmd(url: str, a, dest: str) -> list:
    cmd = [sys.executable, "-m", "yt_dlp", "-v", "--no-playlist", "--no-part", "--ffmpeg-location", FFMPEG_DIR,
           "-f", f"bv*[height<={a.height}]+ba/b[height<={a.height}]/w", "--merge-output-format", "mp4",
           "-o", os.path.join(dest, "%(id)s.%(ext)s")]
    cmd += js_runtime_args(a.js)
    if a.remote_components:
        cmd += ["--remote-components", a.remote_components]
    extractor = []
    if a.clients != "default":
        extractor.append(f"player_client={a.clients}")
    if extractor:
        cmd += ["--extractor-args", "youtube:" + ";".join(extractor)]
    if a.pot_url:
        cmd += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={a.pot_url}"]
    if a.cookies:
        # copy so yt-dlp's cookie write-back never touches the original file
        tmp_cookie = os.path.join(dest, "cookies.txt")
        shutil.copy2(a.cookies, tmp_cookie)
        cmd += ["--cookies", tmp_cookie]
    return cmd + [url]


def interesting(lines):
    keys = ("[pot", "po token", "PO Token", "js runtime", "JS runtime", "[jsc", "n challenge", "nsig",
            "player_client", "Downloading", "ERROR", "WARNING", "Sign in", "403", "format(s)")
    return [l for l in lines if any(k in l for k in keys)][-25:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--label", required=True)
    ap.add_argument("--clients", default="default")
    ap.add_argument("--js", default="default", choices=["default", "node", "deno"])
    ap.add_argument("--remote-components", default="")
    ap.add_argument("--pot-url", default="")
    ap.add_argument("--cookies", default="")
    ap.add_argument("--height", type=int, default=360)
    ap.add_argument("--min-wait", type=float, default=8)
    ap.add_argument("--max-wait", type=float, default=25)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    ver = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"], capture_output=True, text=True).stdout.strip()
    ok_count = 0
    for i, url in enumerate(a.urls):
        if i:
            time.sleep(random.uniform(a.min_wait, a.max_wait))
        dest = tempfile.mkdtemp(prefix="ytprobe_")
        cmd = build_cmd(url, a, dest)
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
        files = [f for f in os.listdir(dest) if f != "cookies.txt" and not f.endswith((".part", ".ytdl"))]
        size = sum(os.path.getsize(os.path.join(dest, f)) for f in files)
        lines = (r.stderr or "").splitlines() + (r.stdout or "").splitlines()
        ok = r.returncode == 0 and size > 10_000
        ok_count += ok
        rec = {"label": a.label, "url": url, "ok": ok, "exit": r.returncode, "seconds": round(time.time() - t0, 1),
               "bytes": size, "files": files, "yt_dlp": ver, "args": {k: v for k, v in vars(a).items() if k != "urls"},
               "cmd": " ".join(c if "cookies" not in c else "<cookies>" for c in cmd[1:]),
               "highlights": interesting(lines), "tail40": lines[-40:]}
        with open(os.path.join(OUT, "probe_results.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[{a.label}] {'OK ' if ok else 'FAIL'} exit={r.returncode} {size / 1e6:.1f} MB {rec['seconds']}s {url}")
        if not ok:
            print("   " + "\n   ".join(lines[-12:]))
        shutil.rmtree(dest, ignore_errors=True)
    print(f"[{a.label}] {ok_count}/{len(a.urls)} succeeded (yt-dlp {ver})")
    sys.exit(0 if ok_count == len(a.urls) else 1)


if __name__ == "__main__":
    main()
