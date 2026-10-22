"""Step A: real source discovery for the 4 test niches (Qwen queries → ytsearch → stats → Qwen assessment
→ Whisper speech sample (budgeted) → saturation → TRIAL / REJECTED)."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

common.activate()
if os.environ.get("FT_DB"):          # re-run on a scratch DB (decisions re-run: A2)
    os.environ["DATABASE_URL"] = "sqlite:///" + os.environ["FT_DB"].replace("\\", "/")
RESULT = os.environ.get("FT_RESULT", "A")
common.set_step(RESULT)

import downloader  # noqa: E402
import models  # noqa: E402
import sources_manager as sm  # noqa: E402
from gpu_lock import gpu_job  # noqa: E402
from models import Decision, Sectors, Source  # noqa: E402
from production import segment_transcript_text  # noqa: E402
from transcriber import WhisperSession, speech_ratio  # noqa: E402

CANDIDATES_PER_NICHE = 3
BUDGET_SKIP = "budget-skip"


class BudgetedYT(sm.YouTubeAPI):
    def __init__(self, budget):
        self.budget = budget
        self.niche = None
        self.sampled = {}
        self.events = []

    def search(self, query, n=50):
        r = super().search(query, n)
        self.events.append({"call": "search", "niche": self.niche, "query": query, "results": len(r)})
        return r

    def channel_videos(self, url, n=15):
        r = super().channel_videos(url, n)
        self.events.append({"call": "channel_videos", "niche": self.niche, "url": url,
                            "entries": len(r.get("entries") or [])})
        return r

    def video_categories(self, video_url):
        r = super().video_categories(video_url)
        self.events.append({"call": "video_categories", "niche": self.niche, "url": video_url, "categories": r})
        return r

    def speech_sample(self, video_url):
        if self.sampled.get(self.niche) or not self.budget.take(f"A speech sample ({self.niche}): {video_url}"):
            self.events.append({"call": "speech_sample", "niche": self.niche, "url": video_url, "skipped": True})
            return 0.0, BUDGET_SKIP
        self.sampled[self.niche] = video_url
        t = time.time()
        path = downloader.download_audio_sample(video_url, 60, 240)
        with gpu_job("whisper_sample"):
            with WhisperSession() as ws:
                res = ws.transcribe(path)
        ratio = speech_ratio(res.get("segments", []), 180.0)
        sample = {"niche": self.niche, "video_url": video_url, "language": res.get("language"),
                  "speech_ratio": ratio, "segments": [{"start": s["start"], "end": s["end"], "text": s["text"]}
                                                      for s in res.get("segments", [])],
                  "transcript": segment_transcript_text(res.get("segments", []))}
        os.makedirs(os.path.join(common.RUN, "results", "samples"), exist_ok=True)
        with open(os.path.join(common.RUN, "results", "samples", f"{downloader.video_id_from_url(video_url)}.json"),
                  "w", encoding="utf-8") as f:
            json.dump(sample, f, indent=2, ensure_ascii=False)
        self.events.append({"call": "speech_sample", "niche": self.niche, "url": video_url, "seconds":
                            round(time.time() - t, 1), "speech_ratio": round(ratio, 3), "language": sample["language"]})
        return ratio, res.get("language", "")

    def delay(self):
        downloader.polite_delay(4, 10)


def main():
    budget = common.Budget()
    yt = BudgetedYT(budget)
    common.instrumented_llm()
    s = models.Session()
    t0 = time.time()
    started = __import__("timeutil").utcnow() - __import__("datetime").timedelta(seconds=5)
    common.sample_resources("A start")
    per_niche = {}
    for sec in s.query(Sectors).filter(Sectors.name.in_(["Podcast", "Personal Finance", "Tech / AI", "Psychology"])).all():
        yt.niche = sec.name
        tn = time.time()
        try:
            out = sm.discover_niche(s, sec, yt=yt, intensify=True, max_candidates=CANDIDATES_PER_NICHE)
        except downloader.YouTubeBlocked as e:
            out = {"error": f"BLOCKED: {e}"}
        # candidates whose sample was skipped for budget are not "rejected for language": park as CANDIDATE
        for src in s.query(Source).filter_by(sector_id=sec.id, language=BUDGET_SKIP).all():
            src.status, src.language = "CANDIDATE", None
            src.status_reason = f"speech sample skipped: test download budget ({budget.limit} downloads)"
        s.commit()
        rows = s.query(Source).filter_by(sector_id=sec.id).all()
        per_niche[sec.name] = {
            "seconds": round(time.time() - tn, 1), "discover_out": out,
            "sources": [{"handle": r.handle, "url": r.channel_url, "title": r.title, "status": r.status,
                         "reason": r.status_reason, "score": r.score, "niche_fit": r.niche_fit,
                         "clip_permission": r.clip_permission, "speech_ratio": r.speech_ratio,
                         "language": r.language, "saturation": r.saturation, "subscribers": r.subscribers,
                         "avg_views": r.avg_views, "avg_duration_s": r.avg_duration_s,
                         "recent_titles": json.loads(r.recent_titles or "[]")[:5]} for r in rows
                        if r.discovered_at and r.discovered_at >= started]}
        print(f"[A] {sec.name}: {[(r['handle'], r['status'], r['score']) for r in per_niche[sec.name]['sources']]}")
    decisions = [{"task": d.task, "proposed": d.proposed_action, "reason": d.reason, "validation": d.validation,
                  "input": d.input_summary} for d in s.query(Decision).filter(Decision.task.in_(
                      ["source_queries", "source_add", "source_remove"]), Decision.created_at >= started).all()]
    result = {"seconds": round(time.time() - t0, 1), "per_niche": per_niche, "decisions": decisions,
              "yt_events": yt.events, "qwen": common.qwen_stats(RESULT), "budget_used": budget.used(),
              "model": __import__("config").settings.OLLAMA_MODEL, "resources": common.sample_resources("A end")}
    common.save_result(RESULT, result)
    print(json.dumps({"seconds": result["seconds"], "qwen": result["qwen"], "budget": len(result["budget_used"])},
                     indent=1))


if __name__ == "__main__":
    main()
