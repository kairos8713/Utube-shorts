"""Step 1e: qwen3:32b vs qwen3.8:27b on identical inputs (A's real Whisper samples + 10 comments).

Measures per model: GPU share / context in `ollama ps` (6144 + 8192 [+ 12288 if asked]), think leakage,
JSON validity, call time, token/s, and keeps every output for side-by-side quality review.
Runs on a scratch DB (bench.db) so the test copy DB stays clean for later steps.
  python step_1e.py                 # full comparison
  python step_1e.py --ps MODEL NUM_GPU CTX [CTX ...]   # only the GPU/context check
"""
import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

RUN = common.activate()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(RUN, "bench.db").replace("\\", "/")

import llm_client  # noqa: E402
import models  # noqa: E402
from comments import BENCH_SUBSET, COMMENTS  # noqa: E402

CONFIGS = [("qwen3:32b", ""), ("qwen3.8:27b", "99")]


def ollama_ps_line(model: str) -> dict:
    out = subprocess.run([llm_client.ollama_exe(), "ps"], capture_output=True, text=True, timeout=30).stdout
    for line in out.splitlines()[1:]:
        if line.split() and llm_client._full_name(line.split()[0]) == llm_client._full_name(model):
            cols = [c for c in line.split("  ") if c.strip()]
            return {"raw": line.strip(), "cols": [c.strip() for c in cols]}
    return {"raw": None}


def ps_check(model: str, num_gpu: str, ctx: int) -> dict:
    os.environ["OLLAMA_NUM_CTX"], os.environ["OLLAMA_NUM_GPU"] = str(ctx), num_gpu
    c = llm_client.OllamaClient(model=model)
    c.unload()
    time.sleep(3)
    t = time.time()
    try:
        c.load(keep_alive="3m")
        share = c.gpu_residency()
        line = ollama_ps_line(model)
        res = {"model": model, "num_gpu": num_gpu or "auto", "num_ctx": ctx, "gpu_share": share,
               "load_s": round(time.time() - t, 1), "ollama_ps": line["raw"],
               "nvidia": common.sample_resources(f"1e ps {model} ctx{ctx} gpu{num_gpu or 'auto'}").get("gpus")}
    except Exception as e:
        res = {"model": model, "num_gpu": num_gpu or "auto", "num_ctx": ctx, "error": str(e)[:300]}
    c.unload()
    time.sleep(3)
    print("[ps]", json.dumps({k: res.get(k) for k in ("model", "num_gpu", "num_ctx", "gpu_share", "ollama_ps")}))
    return res


def samples():
    rows = [json.load(open(p, encoding="utf-8")) for p in glob.glob(os.path.join(RUN, "results", "samples", "*.json"))]
    rows = [r for r in rows if r.get("language") == "en" and r.get("segments")]
    return sorted(rows, key=lambda r: -r["speech_ratio"])[:2]


def clip_text(sample, start, end):
    return " ".join(s["text"].strip() for s in sample["segments"] if s["end"] >= start and s["start"] <= end)


def run_tasks(model: str, num_gpu: str, inputs: list) -> dict:
    import comment_responder as cr
    import qwen_specialized as q
    from crypto_utils import encrypt
    from gpu_lock import gpu_job
    from models import Comment, OwnedChannels, Post, Sectors, videos
    os.environ["OLLAMA_NUM_CTX"], os.environ["OLLAMA_NUM_GPU"] = "6144", num_gpu
    step = f"1e:{model}"
    common.set_step(step)
    client = common.instrumented_llm(model)
    models.Base.metadata.drop_all(models.engine)
    models.Base.metadata.create_all(models.engine)
    s = models.Session()
    out = {"model": model, "num_gpu": num_gpu or "auto", "clips": [], "captions": [], "comments": [], "errors": []}
    t0 = time.time()
    with gpu_job(f"bench {model}"):
        for smp in inputs:
            clips = q.score_clips(smp["transcript"], smp["niche"], 180.0, llm=client)
            out["clips"].append({"sample": smp["video_url"], "niche": smp["niche"], "clips": clips})
            if clips:
                top = clips[0]
                text = clip_text(smp, top["start"], top["end"])
                cap = q.write_caption(text, smp["niche"], "SourceChannel", [], llm=client)
                out["captions"].append({"sample": smp["video_url"], "clip": [top["start"], top["end"]],
                                        "clip_text": text[:600], "caption": cap})
            else:
                out["errors"].append(f"no valid clip for {smp['video_url']}")
        # comment tasks on the first sample's context
        smp = inputs[0]
        sec = Sectors(name=smp["niche"], title="t", description="d", tags="t")
        s.add(sec)
        s.flush()
        acc = OwnedChannels(name="bench", sector_id=sec.id, level=1, mail="-", password=encrypt("-"), status="ACTIVE")
        s.add(acc)
        s.flush()
        cap_text = (out["captions"][0]["caption"] or {}).get("caption", "") if out["captions"] else ""
        v = videos(sector_id=sec.id, owned_channel_id=acc.id, qwen_point=8, transcript=smp["transcript"][:3000],
                   caption=cap_text, qc_status="PASS")
        s.add(v)
        s.flush()
        p = Post(owned_channel_id=acc.id, video_id=v.id, status="PUBLISHED", caption=cap_text)
        s.add(p)
        s.flush()
        for i in BENCH_SUBSET:
            kind, user, text = COMMENTS[i]
            c = Comment(ig_comment_id=f"b{i}", post_id=p.id, owned_channel_id=acc.id, username=user, text=text)
            s.add(c)
            s.flush()
            label = cr.classify_comment(s, c, llm=client)
            reply, err = (None, None)
            if label in ("question", "discussion", "emoji_short"):
                reply, err = cr.write_reply(s, c, llm=client)
            out["comments"].append({"kind": kind, "text": text, "label": label, "reply": reply, "reply_error": err})
            s.commit()
        client.unload()
    out["seconds"] = round(time.time() - t0, 1)
    out["qwen"] = common.qwen_stats(step)
    rows = [json.loads(l) for l in open(os.path.join(RUN, "qwen_calls.jsonl"), encoding="utf-8")]
    rows = [r for r in rows if r["step"] == step]
    out["avg_prompt_tok_s"] = round(sum(r["prompt_tok_s"] or 0 for r in rows) / max(len(rows), 1), 1)
    out["avg_content_chars"] = round(sum(r["content_chars"] for r in rows) / max(len(rows), 1), 1)
    out["thinking_chars_total"] = sum(r["thinking_field_chars"] for r in rows)
    out["first_try_json_ok"] = sum(not r["is_retry"] for r in rows)
    print(f"[{model}] {out['seconds']}s qwen={out['qwen']}")
    return out


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--ps":
        model, num_gpu, ctxs = sys.argv[2], sys.argv[3] if sys.argv[3] != "auto" else "", [int(x) for x in sys.argv[4:]]
        res = [ps_check(model, num_gpu, c) for c in ctxs]
        path = os.path.join(RUN, "results", f"ps_{model.replace(':', '_')}_{num_gpu or 'auto'}.json")
        json.dump(res, open(path, "w", encoding="utf-8"), indent=2)
        return
    inputs = samples()
    if len(inputs) < 2:
        print("need 2 English samples from step A")
        sys.exit(2)
    ps = []
    for model, num_gpu in CONFIGS:
        for ctx in (6144, 8192):
            ps.append(ps_check(model, num_gpu, ctx))
    ps.append(ps_check("qwen3.8:27b", "", 6144))      # Ollama's own placement (Emre measured 6 % CPU)
    results = [run_tasks(model, num_gpu, inputs) for model, num_gpu in CONFIGS]
    common.save_result("1e", {"inputs": [i["video_url"] for i in inputs], "ps": ps, "results": results,
                              "resources": common.sample_resources("1e end")})


if __name__ == "__main__":
    main()
