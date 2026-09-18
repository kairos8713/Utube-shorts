"""ffmpeg_renderer part 2 — outro renderer and main render_short_ffmpeg()"""
import os, math, uuid, time, tempfile, shutil
from typing import Optional, List


def _render_outro_ffmpeg(
    source_path: str,
    clip_duration: float,
    outro_dur: float,
    clip_w: int,
    clip_h: int,
    judgment: str,
    cta_question: str,
    tts_path: Optional[str],
    accent_color_1: str,
    accent_color_3: str,
    font_style: str,
    tmp_dir: str,
) -> Optional[str]:
    """
    Build outro as an intermediate MP4 file.
    Returns path to outro MP4, or None on failure.
    """
    from ffmpeg_renderer import (
        _run_ffmpeg, _make_glass_panel_png, _make_text_png,
        _hex_to_rgb, _encode_flags, _probe_duration,
    )
    from PIL import Image, ImageDraw

    print("[OUTRO] Building outro segment via FFmpeg ...")
    outro_out = os.path.join(tmp_dir, "outro_segment.mp4")

    # ── 1. Background: last N seconds blurred ────────────────────────────────
    replay_start = max(0.0, clip_duration - 5.5)
    replay_end   = max(clip_duration - 0.2, replay_start + 0.5)
    outro_bg_mp4 = os.path.join(tmp_dir, "outro_bg.mp4")
    ok = _run_ffmpeg([
        "-ss", str(replay_start), "-to", str(replay_end),
        "-i", source_path,
        "-vf", f"scale={clip_w}:{clip_h}:flags=lanczos:force_original_aspect_ratio=increase,"
               f"crop={clip_w}:{clip_h},"
               f"gblur=sigma=25,"
               f"colorchannelmixer=rr=0.40:gg=0.40:bb=0.40",
        "-an", "-c:v", "libx264", "-preset", "ultrafast",
        outro_bg_mp4,
    ], label="OUTRO_BG")
    if not ok or not os.path.exists(outro_bg_mp4):
        return None

    # ── 2. Pre-render glassmorphism card PNG ──────────────────────────────────
    r1, g1, b1 = _hex_to_rgb(accent_color_1)
    r3, g3, b3 = _hex_to_rgb(accent_color_3)
    card_w, card_h = min(900, clip_w - 80), 340
    card_x, card_y = (clip_w - card_w) // 2, 790
    card_png = os.path.join(tmp_dir, "outro_card.png")
    _make_glass_panel_png(
        card_w, card_h, card_png,
        bg_color=(12, 12, 18), opacity=191,
        left_border_color=(r1, g1, b1), border_w=3,
        bottom_border_color=(r3, g3, b3),
    )

    # ── 3. Pre-render text PNGs ───────────────────────────────────────────────
    tag_png = os.path.join(tmp_dir, "outro_tag.png")
    _make_text_png("■ ANALYSIS", tag_png,
                   font_name=font_style, font_size=18,
                   color=_hex_to_rgb(accent_color_3),
                   stroke_width=0)

    judgment_png = None
    if judgment and judgment.strip():
        judgment_png = os.path.join(tmp_dir, "outro_judgment.png")
        _make_text_png(judgment.strip()[:200], judgment_png,
                       font_name=font_style, font_size=44,
                       color=(255, 255, 255),
                       max_width=card_w - 40, stroke_width=2)

    cta_png = None
    if cta_question and cta_question.strip():
        cta_png = os.path.join(tmp_dir, "outro_cta.png")
        _make_text_png(cta_question.strip(), cta_png,
                       font_name=font_style, font_size=34,
                       color=(255, 255, 255),
                       max_width=card_w - 40, stroke_width=2)

    # ── 4. Build outro filter_complex ─────────────────────────────────────────
    # Inputs: [0] looped bg, [1] card, [2] tag, [3?] judgment, [4?] cta
    inputs = [
        ["-stream_loop", "-1", "-i", outro_bg_mp4],
        ["-loop", "1", "-i", card_png],
        ["-loop", "1", "-i", tag_png],
    ]
    extra_in = []
    if judgment_png:
        extra_in.append(["-loop", "1", "-i", judgment_png])
    if cta_png:
        extra_in.append(["-loop", "1", "-i", cta_png])

    judgment_idx = 3 if judgment_png else None
    cta_idx      = (3 + (1 if judgment_png else 0)) if cta_png else None

    card_dur   = max(outro_dur - 0.2, 4.5)
    fade_in    = 0.5
    card_x_str = str(card_x)
    card_y_str = str(card_y)

    flt = (
        f"[0:v]trim=end={outro_dur:.4f},setpts=PTS-STARTPTS[bg];"
        f"[1:v]fade=t=in:st=0:d={fade_in}:alpha=1,"
        f"fade=t=out:st={max(card_dur-0.5,0):.3f}:d=0.5:alpha=1,"
        f"setpts=PTS+0/TB[card_f];"
        f"[bg][card_f]overlay=x={card_x_str}:y={card_y_str}:enable='lte(t,{card_dur:.3f})'[v1];"
        f"[2:v]fade=t=in:st=0:d={fade_in}:alpha=1,"
        f"fade=t=out:st={max(card_dur-0.5,0):.3f}:d=0.5:alpha=1,"
        f"setpts=PTS+0/TB[tag_f];"
        f"[v1][tag_f]overlay=x={card_x+16}:y={card_y+10}:enable='lte(t,{card_dur:.3f})'[v2]"
    )
    cur = "v2"
    idx = 3
    if judgment_png:
        flt += (
            f";[{idx}:v]fade=t=in:st=0:d={fade_in}:alpha=1,"
            f"fade=t=out:st={max(card_dur-0.5,0):.3f}:d=0.5:alpha=1,"
            f"setpts=PTS+0/TB[jud_f];"
            f"[{cur}][jud_f]overlay=x={card_x+20}:y={card_y+24}:enable='lte(t,{card_dur:.3f})'[v{idx}]"
        )
        cur = f"v{idx}"
        idx += 1
    if cta_png:
        flt += (
            f";[{idx}:v]fade=t=in:st=0:d={fade_in}:alpha=1,"
            f"fade=t=out:st={max(card_dur-0.5,0):.3f}:d=0.5:alpha=1,"
            f"setpts=PTS+0/TB[cta_f];"
            f"[{cur}][cta_f]overlay=x={card_x+20}:y={card_y+card_h-130}:enable='lte(t,{card_dur:.3f})'[vfinal]"
        )
        cur = "vfinal"

    # ── 5. Assemble ffmpeg command ────────────────────────────────────────────
    cmd_inputs = []
    for inp in inputs + extra_in:
        cmd_inputs.extend(inp)

    audio_args = []
    if tts_path and os.path.exists(tts_path):
        audio_args = ["-i", tts_path,
                      "-c:a", "aac", "-b:a", "192k",
                      "-af", f"afade=t=in:d=0.5,afade=t=out:st={max(outro_dur-0.5,0):.3f}:d=0.5"]
    else:
        audio_args = ["-an"]

    ok = _run_ffmpeg(
        cmd_inputs + (["-i", tts_path] if tts_path and os.path.exists(tts_path) else []) + [
            "-filter_complex", flt,
            "-map", f"[{cur}]",
        ] + ([f"-map", f"{len(inputs+extra_in)}:a",
              "-c:a", "aac", "-b:a", "192k",
              "-af", f"afade=t=in:d=0.5,afade=t=out:st={max(outro_dur-0.5,0):.3f}:d=0.5"]
             if tts_path and os.path.exists(tts_path) else ["-an"]) + [
            "-t", str(outro_dur),
            "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            outro_out,
        ],
        label="OUTRO_COMPOSE",
    )
    return outro_out if ok and os.path.exists(outro_out) else None
