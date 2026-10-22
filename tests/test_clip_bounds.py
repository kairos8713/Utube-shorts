"""Clip start/end are moved onto sentence boundaries from Whisper word timestamps (full test: 6/6 clips
started or ended mid-sentence)."""
import ffmpeg_renderer
from clip_bounds import align_clip, sentence_ends, sentence_starts


def transcript(sentences, t0=1.0, w=0.4, gap=0.1, pause=0.4):
    """Synthetic word timestamps: each sentence = n words of 0.4 s, 0.1 s apart; the last ends with '.'."""
    words, t = [], t0
    for n in sentences:
        for k in range(n):
            words.append({"start": round(t, 3), "end": round(t + w, 3), "text": "word." if k == n - 1 else "word"})
            t += w + gap
        t += pause
    return words


WORDS = transcript([10] * 12)    # sentence starts 1.0, 6.4, 11.8, … ends 5.9, 11.3, 16.7, … 32.9, 38.3


def test_synthetic_transcript_boundaries():
    assert [WORDS[i]["start"] for i in sentence_starts(WORDS)][:3] == [1.0, 6.4, 11.8]
    assert [WORDS[i]["end"] for i in sentence_ends(WORDS)][:3] == [5.9, 11.3, 16.7]


def test_already_on_a_boundary_only_adds_the_end_tail():
    assert align_clip(WORDS, 6.4, 27.5) == ((6.4, 27.75), "")


def test_start_moves_back_and_end_moves_forward():
    # Qwen: 8.0 (1.6 s into a sentence) → back to 6.4; 29.0 (mid-sentence) → forward to 32.9 + 0.25
    assert align_clip(WORDS, 8.0, 29.0) == ((6.4, 33.15), "")


def test_start_moves_forward_and_end_moves_back():
    # no sentence start in the 3 s before 10.5 → forward to 11.8; nothing ≤ 4 s after 33.5 → back to 32.9
    assert align_clip(WORDS, 10.5, 33.5) == ((11.8, 33.15), "")


def test_no_sentence_boundary_rejects():
    one_long_sentence = transcript([80])
    bounds, why = align_clip(one_long_sentence, 10.0, 32.0)
    assert bounds is None and "no sentence start" in why


def test_aligned_length_over_32s_rejects():
    bounds, why = align_clip(WORDS, 6.4, 34.5)          # end snaps to 38.3 → 32.15 s
    assert bounds is None and "outside 18-32" in why


def test_tail_never_runs_into_the_next_word():
    tight = transcript([10] * 6, pause=0.0)               # next sentence starts 0.1 s after the full stop
    (s, e), _ = align_clip(tight, 1.0, 20.9)
    nxt = next(w for w in tight if w["start"] > 20.9)
    assert e == nxt["start"] < 20.9 + 0.25


def test_audio_edges_fade_in_and_out_012s():
    f = ffmpeg_renderer.build_base_filter(10.0, 32.0, 22.0, has_bgm=False)
    assert "afade=t=in:st=0:d=0.12" in f and "afade=t=out:st=21.880:d=0.12" in f
