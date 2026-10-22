"""Start a service (panel.py, publish_runner.py, comment_responder.py, cycle_runner.py, watchdog.py) inside the
test environment: copy DB (or FT_DB), test dirs, DRY_RUN, network guard.

The service module is imported and its main() called (so patches below reach the running code):
  * cycle_runner: no new YouTube downloads (test budget spent; cached files still served), discovery and the
    pip self-update skipped
  * watchdog: restarts go through this wrapper again, so a restarted service can never fall back to the
    real .env database or run without the network guard
  python run_service.py <script.py>
"""
import importlib
import os
import runpy
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

RUN = common.activate()
if os.environ.get("FT_DB"):
    os.environ["DATABASE_URL"] = "sqlite:///" + os.environ["FT_DB"].replace("\\", "/")
script = sys.argv[1]
name = os.path.splitext(os.path.basename(script))[0]
sys.argv = [script]

if name == "panel":
    runpy.run_path(os.path.join(common.ROOT, script), run_name="__main__")
    sys.exit(0)

if name == "cycle_runner":
    import downloader
    import sources_manager

    def cached_only(url, out_dir=None, max_height=1080):
        path = os.path.join(out_dir or os.environ["DOWNLOAD_DIR"], f"{downloader.video_id_from_url(url)}.mp4")
        if os.path.exists(path):
            return path
        raise downloader.DownloadError("full test: download budget spent (cached files only)")

    def no_sample(*a, **k):
        raise downloader.DownloadError("full test: download budget spent")
    downloader.download_youtube_video = cached_only
    downloader.download_audio_sample = no_sample
    downloader.update_ytdlp_once = lambda: None
    sources_manager.cycle_maintenance = lambda session, now=None, llm=None, yt=None: {"skipped": "full test"}

module = importlib.import_module(name)

if name == "watchdog":
    def wrapped_restarter(script_name, pid=None):
        try:
            if pid:
                import psutil
                if psutil.pid_exists(pid):
                    psutil.Process(pid).kill()
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            log = open(os.path.join(RUN, "logs", f"restarted_{os.path.splitext(script_name)[0]}.out"), "a",
                       encoding="utf-8")
            subprocess.Popen([sys.executable, os.path.abspath(__file__), script_name], env=dict(os.environ),
                             stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
            return True
        except Exception as e:
            module.log.error("test restarter failed: %s", e)
            return False
    module.default_restarter = wrapped_restarter

module.main()
