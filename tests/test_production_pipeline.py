import os

import downloader
import render_qc
from llm_mock import MockLLM
from production import Pipeline, SourceVideo, clip_words

EVENTS = []


class FakeWhisper:
    def __init__(self, language="en"):
        self.language = language

    def __call__(self):
        return self

    def __enter__(self):
        EVENTS.append("whisper_load")
        return self

    def __exit__(self, *a):
        EVENTS.append("whisper_unload")

    def transcribe(self, path, words=False):
        EVENTS.append("whisper")
        # 6 sentences of 8 words from 30.0 s; full stops end at 33.9, 37.9, … 53.9
        words_ = [{"start": 30 + i * 0.5, "end": 30.4 + i * 0.5, "word": f"w{i}" + ("." if i % 8 == 7 else "")}
                  for i in range(48)]
        return {"language": self.language,
                "segments": [{"start": 30, "end": 52, "text": "a long interesting story", "words": words_}]}


def fake_split(path):
    return [{"path": path, "offset": 180.0, "duration": 180.0}]


def fake_render(seg_path, start, end, words, handle, out_path, **kw):
    EVENTS.append("render")
    with open(out_path, "wb") as f:
        f.write(b"x" * 10)
    return {"duration": end - start, "rife_used": True}


def qc_pass(path, dur):
    return render_qc.QCResult(True)


def make_pipeline(tmp_path, **kw):
    EVENTS.clear()
    llm = MockLLM()
    orig = llm.chat_json

    def tracked(messages, task, **k):
        EVENTS.append(f"llm:{task}")
        return orig(messages, task, **k)
    llm.chat_json = tracked
    llm.unload = lambda: EVENTS.append("llm_unload")
    defaults = dict(llm=llm, whisper_factory=FakeWhisper(), download_fn=lambda url: EVENTS.append("download") or "/fake.mp4",
                    split_fn=fake_split, render_fn=fake_render, interpolate_fn=lambda a, b: True,
                    qc_fn=qc_pass, delay_fn=lambda: None, out_dir=str(tmp_path))
    defaults.update(kw)
    return Pipeline(**defaults)


def test_stage_order_is_strict(tmp_path):
    ready = []
    p = make_pipeline(tmp_path, on_clip_ready=ready.append)
    srcs = [SourceVideo(url=f"https://youtube.com/watch?v=v{i}", niche="podcast", source_handle="Src") for i in range(2)]
    res = p.run(srcs)
    assert len(res.produced) == 2 and len(ready) == 2
    kinds = [e.split(":")[0] for e in EVENTS]
    # all downloads before whisper, all whisper before llm, llm unloaded before renders
    assert max(i for i, e in enumerate(kinds) if e == "download") < kinds.index("whisper_load")
    assert kinds.index("whisper_unload") < kinds.index("llm")
    assert kinds.index("llm_unload") < kinds.index("render")
    cand = res.produced[0]
    assert cand.abs_start == 210.0 and cand.caption["caption"].endswith("🎥 @Src on YouTube")
    assert (cand.start, cand.end) == (30.0, 54.15)        # Qwen's 30–52 s → end of the sentence at 53.9 + 0.25
    assert cand.words and cand.words[0]["start"] >= 0


def test_youtube_block_stops_downloads(tmp_path):
    calls = []

    def dl(url):
        calls.append(url)
        raise downloader.YouTubeBlocked("BOT_CHECK", "Sign in to confirm you're not a bot")
    p = make_pipeline(tmp_path, download_fn=dl)
    res = p.run([SourceVideo(url="u1", niche="x"), SourceVideo(url="u2", niche="x")])
    assert res.blocked == "BOT_CHECK" and calls == ["u1"] and not res.produced


def test_non_english_source_is_skipped(tmp_path):
    p = make_pipeline(tmp_path, whisper_factory=FakeWhisper(language="tr"))
    res = p.run([SourceVideo(url="u", niche="x")])
    assert not res.produced and "language" in res.source_errors[0].error


def test_duplicate_segments_skipped(tmp_path):
    p = make_pipeline(tmp_path, is_duplicate=lambda url, s, e: True)
    res = p.run([SourceVideo(url="u", niche="x")])
    assert not res.produced and "render" not in EVENTS


def test_qc_failure_not_queued_and_file_removed(tmp_path):
    failed = []
    p = make_pipeline(tmp_path, qc_fn=lambda path, d: render_qc.QCResult(False, ["fps 30 != 60"]),
                      on_clip_failed=lambda c, r: failed.append(r))
    res = p.run([SourceVideo(url="u", niche="x")])
    assert not res.produced and failed and "qc: fps" in failed[0]
    assert not os.path.exists(res.failed[0].out_path)


def test_clip_words_are_relative():
    words = [{"start": 10.0, "end": 10.5, "text": "a"}, {"start": 31.0, "end": 31.4, "text": "b"},
             {"start": 40.0, "end": 40.2, "text": "c"}]
    assert clip_words(words, 30.0, 35.0) == [{"start": 1.0, "end": 1.4, "text": "b"}]


def test_unalignable_clip_is_rejected_and_the_next_candidate_used(tmp_path):
    """Qwen's best clip has no sentence end near its end → rejected (logged); the next one is used."""
    p = make_pipeline(tmp_path)
    orig = p.llm.chat_json

    def two_clips(messages, task, **k):
        if task == "clip_scoring":
            return {"clips": [{"start": "00:30", "end": "00:59", "score": 90, "topic": "best", "reason": "r"},
                              {"start": "00:34", "end": "00:54", "score": 80, "topic": "next", "reason": "r"}]}
        return orig(messages, task, **k)
    p.llm.chat_json = two_clips
    res = p.run([SourceVideo(url="u", niche="x", max_clips=1)])
    assert [c.topic for c in res.produced] == ["next"]
    assert (res.produced[0].start, res.produced[0].end) == (34.0, 54.15)


class LongWhisper(FakeWhisper):
    """120 words from 30 s, a full stop every 8 words (sentences start every 4 s: 30, 34, … 86)."""

    def transcribe(self, path, words=False):
        EVENTS.append("whisper")
        words_ = [{"start": 30 + i * 0.5, "end": 30.4 + i * 0.5, "word": f"w{i}" + ("." if i % 8 == 7 else "")}
                  for i in range(120)]
        return {"language": "en", "segments": [{"start": 30, "end": 90, "text": "two good stories", "words": words_}]}


def test_clips_are_picked_in_the_per_video_ranking_order(tmp_path):
    """Segment scores say A > B; the ranking over the whole video says B first → B is produced."""
    p = make_pipeline(tmp_path, whisper_factory=LongWhisper())
    orig = p.llm.chat_json

    def answers(messages, task, **k):
        EVENTS.append(f"llm:{task}")
        if task == "clip_scoring":
            return {"clips": [{"start": "00:30", "end": "00:52", "score": 95, "topic": "A", "reason": "r"},
                              {"start": "00:58", "end": "01:20", "score": 70, "topic": "B", "reason": "r"}]}
        if task == "clip_ranking":
            return {"ranking": [{"id": "c1", "score": 88}, {"id": "c0", "score": 64}]}
        return orig(messages, task, **k)
    p.llm.chat_json = answers
    res = p.run([SourceVideo(url="u", niche="x", max_clips=1)])
    assert "llm:clip_ranking" in EVENTS
    assert [c.topic for c in res.produced] == ["B"] and res.produced[0].score == 88
    assert (res.produced[0].start, res.produced[0].end) == (58.0, 82.0)   # tail stops at the next word (82.0)
