import os
import subprocess

import pytest

import ffmpeg_renderer as fr
import render_qc
from synthetic_media import make_source_video, make_tone


# ── filter graph / ASS contracts ─────────────────────────────────────────────

def test_ass_safe_area_margins():
    ass = fr.build_kinetic_ass([{"start": 0.0, "end": 0.4, "text": "hello"},
                                {"start": 0.4, "end": 0.9, "text": "MILLION"}])
    assert ",140,140,540,1" in ass
    assert "Style: Default" in ass and "Style: Highlight" in ass
    assert "Dialogue: 0,0:00:00.40,0:00:00.90,Highlight" in ass


def test_ass_escapes_override_braces():
    ass = fr.build_kinetic_ass([{"start": 0, "end": 1, "text": "{\\pos(1,1)}hack"}])
    assert "{\\pos" not in ass


def test_base_filter_audio_chain_and_fps():
    f = fr.build_base_filter(1.0, 21.0, 20.0, has_bgm=True)
    assert "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]" in f
    assert "fps=30" in f
    assert "gblur" in f and "colorchannelmixer" in f      # blurred, darkened bg
    assert "hqdn3d" in f                                  # cinematic polish


def test_final_filter_only_captions_and_credit():
    f = fr.build_final_filter("C:/tmp/x.ass", "SomeChannel")
    assert "subtitles=" in f
    assert "drawtext=text='@SomeChannel'" in f
    assert "x=60:y=590" in f and "fontsize=26" in f and "white@0.75" in f
    assert "fps=60" in f
    for banned in ("overlay", "progress", "drawbox", "loop"):
        assert banned not in f


def test_sanitize_handle():
    assert fr.sanitize_handle("@Bad':Name,{x}") == "BadNamex"
    assert fr.sanitize_handle(None) == ""


def test_no_legacy_overlay_helpers_left():
    for name in ("burn_overlays_ffmpeg", "_make_text_png", "_make_subscribe_button_png",
                 "_panel_with_text", "_render_outro_ffmpeg", "FINAL_W"):
        assert not hasattr(fr, name)


# ── real render (synthetic footage) + QC ─────────────────────────────────────

@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    d = tmp_path_factory.mktemp("render")
    src = make_source_video(str(d / "src.mp4"), duration=12)
    bgm = make_tone(str(d / "bgm.mp3"), duration=20)
    words = [{"start": 0.2 + i * 0.5, "end": 0.6 + i * 0.5, "text": w}
             for i, w in enumerate("this is a synthetic caption test".split())]
    out = str(d / "reel.mp4")
    info = fr.render_reel(src, 2.0, 8.0, words, "TestSource", out, bgm_path=bgm, interpolate_fn=None,
                          work_dir=str(d))
    return out, info


def test_render_reel_passes_qc(rendered):
    out, info = rendered
    assert info["has_captions"] and info["has_bgm"] and not info["rife_used"]
    qc = render_qc.check_video(out, expected_duration=info["duration"])
    assert qc.passed, qc.reasons
    m = qc.metrics
    assert (m["width"], m["height"]) == (1080, 1920)
    assert abs(m["fps"] - 60) < 0.6
    assert m["vcodec"] == "h264" and m["acodec"] == "aac" and m["sample_rate"] == 48000
    assert m["faststart"] is True
    assert -16 <= m["integrated_lufs"] <= -12


def test_qc_rejects_wrong_format(tmp_path):
    src = make_source_video(str(tmp_path / "small.mp4"), duration=4, size="720x1280", fps=30)
    qc = render_qc.check_video(src, expected_duration=4)
    assert not qc.passed
    joined = " ".join(qc.reasons)
    assert "resolution" in joined and "fps" in joined and "faststart" in joined


def test_qc_rejects_black_start_and_duration(tmp_path, rendered):
    src = make_source_video(str(tmp_path / "black.mp4"), duration=6, size="1080x1920", fps=60,
                            black_start=1.5)
    fixed = str(tmp_path / "black_fs.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-c:v", "copy", "-c:a", "aac",
                    "-ar", "48000", "-movflags", "+faststart", fixed], check=True)
    qc = render_qc.check_video(fixed, expected_duration=20)
    joined = " ".join(qc.reasons)
    assert not qc.passed and "black" in joined and "duration" in joined


def test_moov_detection(rendered, tmp_path):
    out, _ = rendered
    assert render_qc.moov_before_mdat(out)
    slow = str(tmp_path / "slow.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", out, "-c", "copy", slow], check=True)
    assert not render_qc.moov_before_mdat(slow)
