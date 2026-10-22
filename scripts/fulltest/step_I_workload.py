"""GPU workload for step I: a real production batch (Whisper → Qwen → render + RIFE) on a cached source video,
against a scratch DB (FT_DB), so services in step I must share the GPU lock with it."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

RUN = common.activate()
os.environ["DATABASE_URL"] = "sqlite:///" + os.environ["FT_DB"].replace("\\", "/")
common.set_step("I-workload")

import production  # noqa: E402
import models  # noqa: E402
from production_store import ProductionStore  # noqa: E402

client = common.instrumented_llm()
s = models.Session()
store = ProductionStore(s, cycle_id=4242)
src = production.SourceVideo(url="https://www.youtube.com/watch?v=FULLTESTWORK", niche="Podcast", max_clips=2,
                             local_path=os.path.join(RUN, "for_I", "workload_source.mp4"), source_handle="workload")
t = time.time()
res = production.Pipeline(llm=client, is_duplicate=store.is_duplicate, previous_captions=store.previous_captions,
                          post_render=store.post_render, on_clip_ready=store.on_clip_ready,
                          on_clip_failed=store.on_clip_failed, delay_fn=lambda: None).run([src])
out = {"seconds": round(time.time() - t, 1), "produced": len(res.produced), "failed": len(res.failed),
       "stage_seconds": res.stage_seconds, "errors": [x.error for x in res.source_errors]}
with open(os.path.join(RUN, "results", "I_workload.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2)
print(json.dumps(out))
