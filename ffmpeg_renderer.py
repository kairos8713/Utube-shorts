"""
ffmpeg_renderer.py  —  Pure FFmpeg + NVENC rendering pipeline
=============================================================
Drop-in replacement for the MoviePy path inside generate_vertical_short().
Activated via USE_FFMPEG_PIPELINE = True in legacy_runner.py.

GPU: h264_nvenc on RTX 3060  |  CPU: PIL text/overlay pre-rendering
"""
import os, sys, math, uuid, time, shutil, subprocess, tempfile, json
from typing import List, Optional

try:
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

try:
    import numpy as np
    _HAS_NP = True
except ImportError:
    _HAS_NP = False

# ── Mirror constants from legacy_runner (same values) ─────────────────────────
OUTPUT_W        = 1080
OUTPUT_H        = 1920
OUTPUT_FPS      = 24
ZONE2_Y_ANCHOR  = int(OUTPUT_H * 0.76)   # 1459 — karaoke zone top
ZONE1_Y_ANCHOR  = int(OUTPUT_H * 0.04)   # 76   — infobox zone top
SLIDE_FADE_DUR  = 0.6
SLIDE_OFFSET_PX = 60
BG_MUSIC_GLOBAL_VOL = 0.09
BG_MUSIC_DUCK_VOL   = 0.03
USE_CINEMATIC_POLISH = True   # Color grade + vignette + grain
USE_KINETIC_TYPOGRAPHY = True # ASS Kinetic Text Animation
USE_DARK_MASK_INTRO = False   # set True to re-enable dim intro
SHOW_ANALYSIS_FOOTER = False  # set True to show "Analysis: " text

SHORTS_DIR = os.path.abspath("shorts")
TTS_DIR    = os.path.abspath("tts_cache")

# ── NVENC encode flags ─────────────────────────────────────────────────────────
_NVENC_FLAGS = [
    "-c:v", "h264_nvenc",
    "-preset", "p4",
    "-tune", "hq",
    "-rc", "vbr",
    "-cq", "23",
    "-b:v", "8M",
    "-maxrate", "12M",
    "-bufsize", "16M",
    "-profile:v", "high",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac",
    "-b:a", "192k",
]

# Fallback CPU flags (used if NVENC unavailable)
_CPU_FLAGS = [
    "-c:v", "libx264",
    "-preset", "fast",
    "-crf", "23",
    "-threads", "0",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac",
    "-b:a", "192k",
]


# ── Detect NVENC availability once at import time ─────────────────────────────
def _nvenc_available() -> bool:
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        return "h264_nvenc" in r.stdout
    except Exception:
        return False

_USE_NVENC = _nvenc_available()


def _encode_flags() -> list:
    flags = _NVENC_FLAGS if _USE_NVENC else _CPU_FLAGS
    enc = "h264_nvenc" if _USE_NVENC else "libx264"
    print(f"[FFMPEG] Encoder: {enc}")
    return flags


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Low-level helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _hex_to_rgb(hex_color: str):
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (255, 0, 0)
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _load_font(name: str, size: int):
    """Load a PIL font with fallback chain."""
    candidates = [
        name.lower() + ".ttf",
        name.lower() + ".otf",
        "impact.ttf",
        "arial.ttf",
        "DejaVuSans-Bold.ttf",
    ]
    for fname in candidates:
        try:
            return ImageFont.truetype(fname, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _run_ffmpeg(args: list, label: str = "FFMPEG") -> bool:
    """Run an ffmpeg command; log FULL stderr on failure. Returns True on success."""
    cmd = ["ffmpeg", "-y"] + args
    # Log the full command for debugging
    print(f"[{label}] CMD: {' '.join(str(a) for a in cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            print(f"[{label}] ERROR (exit {r.returncode}) — FULL STDERR:")
            print(r.stderr)   # Print ALL stderr, not just last 1500 chars
            return False
        return True
    except subprocess.TimeoutExpired as e:
        print(f"[{label} TIMEOUT] Process killed after 180s")
        print(f"[{label} TIMEOUT] Last stderr: {e.stderr.decode() if e.stderr and isinstance(e.stderr, bytes) else e.stderr or 'none'}")
        raise RuntimeError(f"[{label}] Hung - killed by timeout")
    except Exception as e:
        print(f"[{label}] Exception: {e}")
        return False


def _probe_duration(path: str) -> float:
    """Return video duration via ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", path],
            capture_output=True, text=True, timeout=30,
        )
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0

def _probe_audio_duration(audio_path: str) -> float:
    """Get duration of an audio file in seconds via ffprobe."""
    try:
        r = subprocess.run([
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            audio_path
        ], capture_output=True, text=True, timeout=15)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def _extract_audio_for_whisper(video_path: str, start: float,
                                end: float, out_mp3: str) -> bool:
    """Extract audio slice to mp3 for Whisper — replaces write_audiofile()."""
    dur = max(end - start - 0.2, 0.5)
    return _run_ffmpeg([
        "-ss", str(start), "-t", str(dur), "-i", video_path,
        "-vn", "-c:a", "libmp3lame", "-q:a", "4",
        out_mp3,
    ], label="AUDIO_EXTRACT")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1b — FFmpeg drawtext helpers (no PIL, no PNG)
# ═══════════════════════════════════════════════════════════════════════════════

def _ffmpeg_escape(text: str) -> str:
    """Escape special chars for FFmpeg drawtext (text= parameter)."""
    if not text:
        return ""
    # Order matters: backslash MUST be first
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "\\\\\\'")
    text = text.replace(":", "\\:")
    text = text.replace("%", "\\%")
    text = text.replace(",", "\\,")
    return text


def _safe_drawtext_text(s: str) -> str:
    """Make text unconditionally safe for FFmpeg drawtext on Windows.
    Uses Unicode replacements instead of backslash escaping — far more
    reliable across Windows cmd / filter_complex string boundaries.
    """
    if not s:
        return ""
    s = s.replace("\\", "")          # strip backslashes entirely
    s = s.replace("'", "\u2019")    # straight apostrophe → curly ’
    s = s.replace('"', "\u201D")    # straight quote → curly ”
    s = s.replace("%", " percent") # avoid %% expansion
    s = s.replace(":", " -")       # colons confuse filter parser
    s = s.replace(",", " ")        # commas are filter separators
    return s


def _wrap_text(text: str, max_chars_per_line: int = 28) -> str:
    """Word-wrap text into multiple lines with explicit newlines."""
    if not text:
        return ""
    words = text.split()  # split() collapses all whitespace including double-spaces
    lines = []
    cur = []
    cur_len = 0
    for w in words:
        if cur_len + len(w) + 1 > max_chars_per_line and cur:
            lines.append(" ".join(cur))
            cur = [w]
            cur_len = len(w)
        else:
            cur.append(w)
            cur_len += len(w) + 1
    if cur:
        lines.append(" ".join(cur))
    import re
    result = "\n".join(lines)
    # Normalize: collapse any remaining multiple spaces to one per line
    result = re.sub(r'  +', ' ', result)
    return result


def _drawtext_filter(text: str, x: str, y: str, fontsize: int = 56,
                     color: str = "white", anchor: str = "center",
                     fade_in: float = 0.3, fade_out: float = 0.3,
                     start: float = 0.0, end: float = 999.0,
                     max_chars: int = 28) -> str:
    """
    Build a single FFmpeg drawtext filter string with:
    - Word wrapping via explicit newlines
    - Soft drop shadow (2px offset)
    - Thin black outline
    - Fade in/out alpha animation
    - Center anchoring (uses text_w and text_h)

    Returns the drawtext filter expression (no leading/trailing semicolon).
    """
    wrapped = _wrap_text(text, max_chars_per_line=max_chars)
    escaped = _ffmpeg_escape(wrapped)

    if anchor == "center":
        x_expr = "(w-text_w)/2"
    elif anchor == "left":
        x_expr = x
    elif anchor == "right":
        x_expr = f"w-text_w-{x}"
    else:
        x_expr = x

    # alpha animation: fade in [start, start+fade_in], hold, fade out [end-fade_out, end]
    fi_end = start + fade_in
    fo_start = end - fade_out
    alpha_expr = (
        f"if(lt(t,{start}),0,"
        f"if(lt(t,{fi_end}),(t-{start})/{fade_in},"
        f"if(lt(t,{fo_start}),1,"
        f"if(lt(t,{end}),({end}-t)/{fade_out},0))))"
    )

    return (
        f"drawtext=text='{escaped}'"
        f":fontfile=C\\\\:/Windows/Fonts/arialbd.ttf"
        f":fontsize={fontsize}"
        f":fontcolor={color}"
        f":x='{x_expr}'"
        f":y={y}"
        f":borderw=3"
        f":bordercolor=black@0.8"
        f":shadowx=2:shadowy=3:shadowcolor=black@0.6"
        f":line_spacing=-10"
        f":enable='between(t,{start},{end})'"
    )


def _panel_with_text(text: str, bg_y: int, bg_w: int, bg_h_min: int,
                     fontsize: int = 56, accent_color: str = "0xFF6B00",
                     bg_opacity: float = 0.78,
                     start: float = 0.0, end: float = 999.0,
                     fade_in: float = 0.3, fade_out: float = 0.3,
                     max_chars: int = 24,
                     text_color: str = "white") -> list:
    """
    Builds a list of filter chain entries for a card:
    - Drawbox: semi-transparent dark background panel (centered horizontally)
    - Drawbox: left accent stripe (6px wide, accent color)
    - Drawtext: text centered inside, with shadow and border

    bg_h_min is the MINIMUM panel height. Actual height auto-grows with text lines.
    Returns a LIST of filter strings (caller applies each as a chained step).
    """
    # Pre-compute x as a plain Python int (drawbox doesn't support w-based expressions)
    VIDEO_W = OUTPUT_W   # 1080
    bg_x = (VIDEO_W - bg_w) // 2
    enable_expr = f"between(t,{start},{end})"

    fi_end   = start + fade_in
    fo_start = end - fade_out
    alpha_expr = (
        f"if(lt(t,{start}),0,"
        f"if(lt(t,{fi_end}),(t-{start})/{fade_in},"
        f"if(lt(t,{fo_start}),1,"
        f"if(lt(t,{end}),({end}-t)/{fade_out},0))))"
    )

    # Calculate actual panel height based on wrapped line count
    wrapped = _wrap_text(text, max_chars_per_line=max_chars)
    num_lines = wrapped.count("\n") + 1
    line_height = int(fontsize * 1.25)
    padding_v   = 40   # 20px top + 20px bottom
    calc_h = num_lines * line_height + padding_v
    bg_h = max(bg_h_min, calc_h)

    # Semi-transparent dark panel
    panel_box = (
        f"drawbox=x={bg_x}:y={bg_y}:w={bg_w}:h={bg_h}"
        f":color=0x0F1419@{bg_opacity}:t=fill"
        f":enable='{enable_expr}'"
    )

    # Accent left stripe (6px wide, full panel height)
    accent_stripe = (
        f"drawbox=x={bg_x}:y={bg_y}:w=6:h={bg_h}"
        f":color={accent_color}:t=fill"
        f":enable='{enable_expr}'"
    )

    # Sanitize text through safe Unicode replacement (reliable on Windows)
    safe_text = _safe_drawtext_text(wrapped)
    text_y = f"{bg_y}+({bg_h}-text_h)/2"

    text_filter = (
        f"drawtext=text='{safe_text}'"
        f":fontfile=C\\\\:/Windows/Fonts/arialbd.ttf:fontsize={fontsize}:fontcolor={text_color}"
        f":x='(w-text_w)/2'"
        f":y='{text_y}'"
        f":borderw=2:bordercolor=black@0.9"
        f":shadowx=2:shadowy=3:shadowcolor=black@0.7"
        f":line_spacing=-4"
        f":enable='{enable_expr}'"
    )

    return [panel_box, accent_stripe, text_filter]



# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — PIL overlay renderers (replace TextClip / ImageMagick)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_pill_png(text: str, out_path: str,
                   font_size: int = 58, max_width: int = 960) -> bool:
    """Render a karaoke subtitle pill — black semi-transparent RGBA PNG."""
    if not _HAS_PIL or not text.strip():
        return False
    try:
        font = _load_font("impact", font_size)
        dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        bbox  = dummy.textbbox((0, 0), text, font=font)
        tw    = min(bbox[2] - bbox[0], max_width - 64)
        th    = bbox[3] - bbox[1]
        px, py = 32, 16
        w, h  = tw + px * 2, th + py * 2
        img   = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw  = ImageDraw.Draw(img)
        draw.rounded_rectangle([(0,0),(w-1,h-1)], radius=h//2, fill=(0,0,0,204))
        draw.text((px, py), text, fill=(255,255,255,255), font=font)
        img.save(out_path, "PNG")
        return True
    except Exception as e:
        print(f"[PILL_PNG] Failed '{text[:20]}': {e}")
        return False


def _make_glass_panel_png(width: int, height: int, out_path: str,
                          fill_rgba=(15, 20, 35, 215),
                          accent_rgb=(80, 200, 255),
                          radius: int = 24,
                          glow: bool = True,
                          **kwargs) -> str:
    """
    Modern glassmorphism panel:
    - Dark translucent fill with subtle vertical gradient
    - Rounded corners
    - Accent color glow border (outer + inner)
    - Soft drop shadow (rendered as larger blurred rect behind)
    """
    if not _HAS_PIL:
        return False
    try:
        from PIL import Image, ImageDraw, ImageFilter
        
        if "left_border_color" in kwargs and kwargs["left_border_color"]:
            accent_rgb = kwargs["left_border_color"]
            
        pad = 40
        canvas_w = width + pad * 2
        canvas_h = height + pad * 2
        img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        
        shadow = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        sd.rounded_rectangle(
            [pad + 4, pad + 8, pad + width + 4, pad + height + 8],
            radius=radius, fill=(0, 0, 0, 140)
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=12))
        img.alpha_composite(shadow)
        
        if glow:
            glow_img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow_img)
            gd.rounded_rectangle(
                [pad - 2, pad - 2, pad + width + 2, pad + height + 2],
                radius=radius + 2,
                outline=(*accent_rgb, 180), width=4
            )
            glow_img = glow_img.filter(ImageFilter.GaussianBlur(radius=6))
            img.alpha_composite(glow_img)
        
        panel = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        pd = ImageDraw.Draw(panel)
        
        top_rgba = tuple(min(255, c + 12) for c in fill_rgba[:3]) + (fill_rgba[3],)
        bot_rgba = fill_rgba
        for y in range(height):
            ratio = y / max(1, height - 1)
            r = int(top_rgba[0] * (1 - ratio) + bot_rgba[0] * ratio)
            g = int(top_rgba[1] * (1 - ratio) + bot_rgba[1] * ratio)
            b = int(top_rgba[2] * (1 - ratio) + bot_rgba[2] * ratio)
            a = int(top_rgba[3] * (1 - ratio) + bot_rgba[3] * ratio)
            pd.line([(0, y), (width, y)], fill=(r, g, b, a))
        
        mask = Image.new("L", (width, height), 0)
        md = ImageDraw.Draw(mask)
        md.rounded_rectangle([0, 0, width, height], radius=radius, fill=255)
        panel.putalpha(mask)
        
        img.paste(panel, (pad, pad), panel)
        
        inner = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        id_ = ImageDraw.Draw(inner)
        id_.rounded_rectangle(
            [pad + 1, pad + 1, pad + width - 1, pad + height - 1],
            radius=radius - 1,
            outline=(255, 255, 255, 35), width=1
        )
        img.alpha_composite(inner)
        
        bar = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        bd = ImageDraw.Draw(bar)
        bd.rounded_rectangle(
            [pad, pad + 8, pad + 4, pad + height - 8],
            radius=2, fill=(*accent_rgb, 255)
        )
        img.alpha_composite(bar)
        
        img.save(out_path, "PNG")
        return out_path
    except Exception as e:
        print(f"[GLASS_PNG] Failed: {e}")
        return False


def _make_text_png(text: str, out_path: str,
                   font_name: str = "impact", font_size: int = 48,
                   color=(255,255,255), max_width: int = 1020,
                   stroke_width: int = 2, stroke_color=(0,0,0),
                   align: str = "center") -> Optional[tuple]:
    """
    Render wrapped text to an RGBA PNG.
    Returns (img_width, img_height) or None on failure.
    """
    if not _HAS_PIL or not text.strip():
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
        
        font = None
        for candidate in ["Inter-Bold.ttf", "Poppins-Bold.ttf", "Montserrat-Bold.ttf",
                          "arialbd.ttf", font_name, "arial.ttf"]:
            try:
                font = ImageFont.truetype(candidate, font_size)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()

        words  = text.split()
        lines  = []
        cur    = []
        dummy  = ImageDraw.Draw(Image.new("RGBA", (1,1)))
        for w in words:
            test = " ".join(cur + [w])
            bb   = dummy.textbbox((0,0), test, font=font)
            if bb[2] - bb[0] > max_width and cur:
                lines.append(" ".join(cur))
                cur = [w]
            else:
                cur.append(w)
        if cur:
            lines.append(" ".join(cur))

        line_h = dummy.textbbox((0,0), "Ag", font=font)[3] + 4
        tot_h  = line_h * len(lines) + stroke_width * 2 + 10
        tot_w  = max(
            dummy.textbbox((0,0), ln, font=font)[2] for ln in lines
        ) + stroke_width * 2 + 10

        img  = Image.new("RGBA", (tot_w, tot_h), (0,0,0,0))
        draw = ImageDraw.Draw(img)
        y = 0
        for ln in lines:
            lw = dummy.textbbox((0,0), ln, font=font)[2]
            if align == "center":
                x = (tot_w - lw) // 2
            elif align == "right":
                x = tot_w - lw
            else:
                x = 0
            
            shadow_layer = Image.new("RGBA", img.size, (0,0,0,0))
            sd = ImageDraw.Draw(shadow_layer)
            sd.text((x+2, y+3), ln, font=font, fill=(0,0,0,180))
            shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=3))
            img.alpha_composite(shadow_layer)
            
            draw.text((x, y), ln, font=font, fill=color+(255,),
                      stroke_width=stroke_width, stroke_fill=stroke_color+(255,))
            
            y += line_h

        img.save(out_path, "PNG")
        return (tot_w, tot_h)
    except Exception as e:
        print(f"[TEXT_PNG] Failed '{text[:30]}': {e}")
        return None


def _make_subscribe_button_png(channel_name: str, theme_color: str,
                                out_path: str,
                                video_width: int = OUTPUT_W) -> Optional[tuple]:
    """Render subscribe button — returns (w, h) or None."""
    if not _HAS_PIL:
        return None
    try:
        btn_w  = int(video_width * 0.37)
        btn_h  = max(80, int(btn_w * 0.22))
        bg_col = _hex_to_rgb(theme_color)
        img    = Image.new("RGBA", (btn_w, btn_h), (0,0,0,0))
        draw   = ImageDraw.Draw(img)
        draw.rounded_rectangle([(0,0),(btn_w-1,btn_h-1)],
                                radius=int(btn_h*0.25),
                                fill=bg_col+(235,))
        label  = f"▶  Subscribe\n{channel_name[:24]}"
        fs     = max(18, btn_h // 4)
        font   = _load_font("impact", fs)
        bb     = draw.multiline_textbbox((0,0), label, font=font, align="center")
        tw, th = bb[2]-bb[0], bb[3]-bb[1]
        draw.multiline_text(
            ((btn_w-tw)//2, (btn_h-th)//2),
            label, fill=(255,255,255,255), font=font, align="center",
        )
        img.save(out_path, "PNG")
        return (btn_w, btn_h)
    except Exception as e:
        print(f"[BTN_PNG] Failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — ASS subtitle file generator (replaces karaoke ImageClip pile)
# ═══════════════════════════════════════════════════════════════════════════════

def _seconds_to_ass_time(s: float) -> str:
    h   = int(s // 3600)
    m   = int((s % 3600) // 60)
    sec = s % 60
    cs  = int((sec - int(sec)) * 100)
    return f"{h}:{m:02d}:{int(sec):02d}.{cs:02d}"


def _emphasis_score(word: str) -> str:
    """Return style name based on word characteristics."""
    w = word.strip(".,!?;:\"'").upper()
    POWER_WORDS = {
        "NEVER", "ALWAYS", "STOP", "NOW", "MUST", "ONLY", "BEST", "WORST",
        "WRONG", "RIGHT", "RICH", "POOR", "FAIL", "WIN", "LOSE", "MILLION", "BILLION",
        "FREE", "TRUTH", "LIE", "SECRET", "PROVEN", "GUARANTEED",
    }
    if word.isupper() and len(word) > 2:
        return "Highlight"
    if any(c.isdigit() for c in word) or "%" in word or "$" in word:
        return "Highlight"
    if w in POWER_WORDS:
        return "Highlight"
    if len(word) >= 7:
        return "Pop"
    return "Default"

def _format_time_ass(seconds: float) -> str:
    """Convert seconds to ASS time format H:MM:SS.CC (centiseconds)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    cs = int((s - int(s)) * 100)
    return f"{h}:{m:02d}:{int(s):02d}.{cs:02d}"

def _word_to_dialogue(word_data: dict, video_w: int = 1080) -> str:
    """Convert a single word_segment to an ASS Dialogue line with kinetic animation."""
    word = (word_data.get('word') or word_data.get('text') or '').strip()
    if not word:
        return ""
    start = float(word_data.get('start', 0.0))
    end = float(word_data.get('end', start + 0.3))
    if end - start < 0.20:
        end = start + 0.20
    style = _emphasis_score(word)
    duration_ms = int((end - start) * 1000)
    pop_in_ms = min(120, duration_ms // 3)
    bounce_back_ms = min(80, duration_ms // 5)
    tags = (
        f"{{\\fad(60,80)"
        f"\\fscx60\\fscy60"
        f"\\t(0,{pop_in_ms},\\fscx115\\fscy115)"
        f"\\t({pop_in_ms},{pop_in_ms+bounce_back_ms},\\fscx100\\fscy100)"
        f"}}"
    )
    return f"Dialogue: 0,{_format_time_ass(start)},{_format_time_ass(end)},{style},,0,0,0,,{tags}{word}"

def _build_kinetic_ass(word_segments: list, video_w: int = 1080, video_h: int = 1920) -> str:
    """Build a complete .ass file string with kinetic typography for word_segments."""
    if word_segments:
        first_keys = list(word_segments[0].keys())
        print(f"[ASS] word_segments[0] keys: {first_keys}")
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,68,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,4,2,2,40,40,420,1
Style: Pop,Arial,76,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,5,3,2,40,40,420,1
Style: Highlight,Arial,82,&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,5,3,2,40,40,420,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogues = "\n".join(filter(None, [_word_to_dialogue(w, video_w) for w in word_segments]))
    return header + dialogues + "\n"

def _build_legacy_ass(word_segments: List[dict], clip_duration: float, font_name: str, font_size: int) -> str:
    words_per = 4
    groups = []
    for i in range(0, len(word_segments), words_per):
        chunk = word_segments[i:i+words_per]
        text  = " ".join((w.get("word") or w.get("text") or "").strip() for w in chunk).strip()
        if not text:
            continue
        gs = float(chunk[0].get("start", 0))
        ge = float(chunk[-1].get("end", gs + 0.5))
        ge = min(ge, clip_duration - 0.1)
        if gs >= clip_duration:
            continue
        groups.append((text, gs, ge))

    ass_y  = int(OUTPUT_H * 0.76)
    margin = (OUTPUT_H - ass_y)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUTPUT_W}
PlayResY: {OUTPUT_H}
WrapStyle: 0

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Pill,{font_name},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,-1,0,0,0,100,100,0,0,3,2,0,2,20,20,{margin},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    lines = [header]
    for text, gs, ge in groups:
        s = _seconds_to_ass_time(gs)
        e = _seconds_to_ass_time(ge)
        safe = text.replace("\\", "\\\\").replace("{", "\\{")
        lines.append(f"Dialogue: 0,{s},{e},Pill,,0,0,0,,{safe}")
    return "\n".join(lines)

def make_ass_file(word_segments: List[dict], out_path: str,
                  clip_duration: float,
                  font_name: str = "Impact",
                  font_size: int = 48) -> bool:
    try:
        if USE_KINETIC_TYPOGRAPHY:
            ass_content = _build_kinetic_ass(word_segments, video_w=OUTPUT_W, video_h=OUTPUT_H)
            print(f"[ASS] Kinetic typography: {len(word_segments)} animated words")
        else:
            ass_content = _build_legacy_ass(word_segments, clip_duration, font_name, font_size)
            print(f"[ASS] Legacy typography: {len(word_segments)} words grouped")
            
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(ass_content)
        return True
    except Exception as e:
        import traceback
        print(f"[ASS] Failed to build ASS file: {e}")
        traceback.print_exc()
        raise


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — FFmpeg expression helpers for slide animations
# ═══════════════════════════════════════════════════════════════════════════════

def _ease_out_expr(t_start: float, slide_dur: float, slide_px: int,
                   base_coord: int, axis: str = "y",
                   direction: str = "up") -> str:
    """
    Returns an FFmpeg overlay coordinate expression implementing
    ease-out quadratic slide animation.

    direction='up'   → starts below base_coord, slides up
    direction='left' → starts right of screen, slides left
    direction='down' → starts above base_coord, slides down
    """
    sd = max(slide_dur, 0.001)
    # progress = clamp((t - t_start) / slide_dur, 0, 1)
    # eased    = progress * (2 - progress)
    # offset   = slide_px * (1 - eased)
    p = f"min(max((t-{t_start:.4f})/{sd:.4f},0),1)"
    e = f"{p}*(2-{p})"
    offset = f"{slide_px}*(1-({e}))"

    if direction == "up":
        return f"{base_coord}+({offset})"
    elif direction == "down":
        return f"{base_coord}-({offset})"
    elif direction == "left":
        return f"{base_coord}-{OUTPUT_W}*(1-({e}))"
    return str(base_coord)


def _slide_from_top_expr(t_start: float, base_y: int,
                          slide_dur: float = 0.5, slide_px: int = 80) -> str:
    return _ease_out_expr(t_start, slide_dur, slide_px, base_y,
                          axis="y", direction="up")


def _slide_from_left_expr(t_start: float, slide_dur: float = 0.5) -> str:
    """Panel slides in from left edge."""
    sd = max(slide_dur, 0.001)
    p = f"min(max((t-{t_start:.4f})/{sd:.4f},0),1)"
    e = f"{p}*(2-{p})"
    return f"30*(({e})-1)"   # starts at x=-panel_w, lands at x=30 approximately


def _slide_from_bottom_expr(t_start: float, base_y: int,
                             slide_dur: float = SLIDE_FADE_DUR,
                             slide_px: int = SLIDE_OFFSET_PX) -> str:
    return _ease_out_expr(t_start, slide_dur, slide_px, base_y,
                          axis="y", direction="up")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Overlay filter builder
# ═══════════════════════════════════════════════════════════════════════════════

class _FilterBuilder:
    """
    Incrementally builds an FFmpeg filter_complex string and input list.
    Each overlay step consumes the previous video label and emits a new one.
    """
    def __init__(self, fps: int = OUTPUT_FPS):
        self.inputs: List[list] = []   # list of ['-loop','1','-i','path'] etc.
        self.filters: List[str] = []
        self._vid = "base"             # current video stream label
        self._ctr = 0                  # unique label counter
        self.fps  = fps

    def _new_label(self) -> str:
        self._ctr += 1
        return f"v{self._ctr}"

    def add_input(self, *flags) -> int:
        """Add an input group; return its index (0-based)."""
        idx = len(self.inputs)
        self.inputs.append(list(flags))
        return idx

    def add_filter_step(self, filter_str: str,
                        in_label: Optional[str] = None,
                        extra_inputs: str = "") -> str:
        """
        Append a filter step consuming current video.
        Returns the new output label.
        """
        src = in_label if in_label else self._vid
        out = self._new_label()
        self.filters.append(
            f"[{src}]{extra_inputs}{filter_str}[{out}]"
        )
        self._vid = out
        return out

    def overlay_png(self, input_idx: int, x: str, y: str,
                    t_start: float, t_end: float,
                    fade_in: float = 0.0, fade_out: float = 0.0,
                    w: Optional[int] = None, h: Optional[int] = None) -> str:
        """
        Overlay a static PNG input (by index) at position (x, y)
        enabled between t_start and t_end, with optional fades.
        """
        out  = self._new_label()
        dur  = max(t_end - t_start, 0.1)
        # Build the per-overlay stream label from input index
        # We chain: [Nidx:v] → scale (optional) → fade → setpts → [overlay_N]
        stream = f"{input_idx}:v"
        tmp    = f"ov{self._ctr}_pre"

        chain_parts = []
        if w and h:
            chain_parts.append(f"scale={w}:{h}")
        if fade_in > 0:
            chain_parts.append(f"fade=t=in:st=0:d={fade_in:.3f}:alpha=1")
        if fade_out > 0:
            chain_parts.append(
                f"fade=t=out:st={max(dur-fade_out,0):.3f}:d={fade_out:.3f}:alpha=1"
            )
        chain_parts.append(f"setpts=PTS+{t_start:.4f}/TB")

        self.filters.append(
            f"[{stream}]{',' .join(chain_parts)}[{tmp}]"
        )
        enable = f"between(t,{t_start:.4f},{t_end:.4f})"
        self.filters.append(
            f"[{self._vid}][{tmp}]overlay=x={x}:y={y}:enable='{enable}'[{out}]"
        )
        self._vid = out
        return out

    def drawtext(self, text: str, x: str, y: str,
                 fontfile: str = "arial.ttf",
                 fontsize: int = 24,
                 fontcolor: str = "white@0.85",
                 shadowx: int = 1, shadowy: int = 1) -> str:
        """Add a drawtext filter step."""
        safe = (text.replace("'", "\\'")
                    .replace(":", "\\:")
                    .replace("[", "\\[")
                    .replace("]", "\\]"))
        fstr = (f"drawtext=text='{safe}':fontfile={fontfile}"
                f":fontsize={fontsize}:fontcolor={fontcolor}"
                f":x={x}:y={y}:shadowx={shadowx}:shadowy={shadowy}"
                f":shadowcolor=black")
        return self.add_filter_step(fstr)

    @property
    def current_video(self) -> str:
        return self._vid

    def _apply_cinematic_polish(self):
        """Apply color grading + grain (chromatic aberration omitted for stability)."""
        # Step 1: color grade + grain (single filter chain)
        out = self._new_label()
        self.filters.append(
            f"[{self._vid}]"
            f"hqdn3d=luma_spatial=4:chroma_spatial=3:luma_tmp=6:chroma_tmp=4.5,"
            f"eq=contrast=1.08:saturation=1.15:brightness=-0.02,"
            f"colorbalance=rs=-0.05:bs=0.12:rh=0.08:bh=-0.05,"
            f"unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount=0.6"
            f":chroma_msize_x=5:chroma_msize_y=5:chroma_amount=0.3,"
            f"noise=alls=4:allf=t"
            f"[{out}]"
        )
        self._vid = out
        return out

    def build(self) -> str:
        return ";".join(self.filters)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — Outro renderer
# ═══════════════════════════════════════════════════════════════════════════════

def _render_outro_ffmpeg(source_path, clip_duration, outro_dur,
                         clip_w, clip_h, judgment, cta_question,
                         tts_path, accent_color_1, accent_color_3,
                         font_style, tmp_dir):
    print("[OUTRO] Building outro segment via FFmpeg ...")
    outro_out = os.path.join(tmp_dir, "outro_segment.mp4")
    replay_start = max(0.0, clip_duration - 5.5)
    replay_end   = max(clip_duration - 0.2, replay_start + 0.5)
    outro_bg = os.path.join(tmp_dir, "outro_bg.mp4")
    ok = _run_ffmpeg([
        "-ss", str(replay_start), "-to", str(replay_end), "-i", source_path,
        "-vf", (f"scale={clip_w}:{clip_h}:flags=lanczos"
                f":force_original_aspect_ratio=increase,"
                f"crop={clip_w}:{clip_h},"
                f"gblur=sigma=25,"
                f"colorchannelmixer=rr=0.40:gg=0.40:bb=0.40"),
        "-an", "-c:v", "libx264", "-preset", "ultrafast", outro_bg,
    ], label="OUTRO_BG")
    if not ok:
        return None

    # Build outro_segment.mp4 with drawbox+drawtext card (NO PNG)
    card_y   = 700
    card_w   = 1000
    card_h   = 520
    card_x   = (OUTPUT_W - card_w) // 2   # 40 — plain int, drawbox can't do (w-1000)/2
    sep_x    = card_x + 30
    card_enable = f"between(t,0.1,{outro_dur:.3f})"

    filter_parts = [
        f"[0:v]trim=end={outro_dur:.3f},setpts=PTS-STARTPTS[bg0]"
    ]
    cur = "bg0"

    # 1) Card background — semi-transparent dark
    filter_parts.append(
        f"[{cur}]drawbox=x={card_x}:y={card_y}:w={card_w}:h={card_h}"
        f":color=0x0F1419@0.85:t=fill:enable='{card_enable}'[step1]"
    )
    cur = "step1"

    # 2) Green accent stripe on left (8px wide)
    filter_parts.append(
        f"[{cur}]drawbox=x={card_x}:y={card_y}:w=8:h={card_h}"
        f":color=0x1DB954:t=fill:enable='{card_enable}'[step2]"
    )
    cur = "step2"

    # 3) ANALYSIS tag (top-left of card)
    filter_parts.append(
        f"[{cur}]drawtext=text='ANALYSIS'"
        f":fontfile=C\\\\:/Windows/Fonts/arialbd.ttf:fontsize=28:fontcolor=0x1DB954"
        f":x={sep_x}:y={card_y + 20}"
        f":borderw=1:bordercolor=black@0.6"
        f":enable='{card_enable}'[step3]"
    )
    cur = "step3"

    # 4) Thin separator line between judgment and CTA
    sep_y = card_y + 280
    filter_parts.append(
        f"[{cur}]drawbox=x={sep_x}:y={sep_y}:w={card_w - 60}:h=2"
        f":color=white@0.2:t=fill:enable='{card_enable}'[step4]"
    )
    cur = "step4"

    # 5) Judgment text — centered in upper half of card
    if judgment and judgment.strip():
        jud_wrapped = _wrap_text(judgment.strip(), max_chars_per_line=24)
        jud_safe    = _safe_drawtext_text(jud_wrapped)
        filter_parts.append(
            f"[{cur}]drawtext=text='{jud_safe}'"
            f":fontfile=C\\\\:/Windows/Fonts/arialbd.ttf:fontsize=50:fontcolor=white"
            f":x='(w-text_w)/2'"
            f":y='{card_y + 80}+(180-text_h)/2'"
            f":borderw=2:bordercolor=black@0.8"
            f":shadowx=2:shadowy=3:shadowcolor=black@0.6"
            f":line_spacing=-4"
            f":enable='{card_enable}'[step5]"
        )
        cur = "step5"

    # 6) CTA text — centered in lower half of card (greenish tint)
    if cta_question and cta_question.strip():
        cta_wrapped = _wrap_text(cta_question.strip(), max_chars_per_line=28)
        cta_safe    = _safe_drawtext_text(cta_wrapped)
        filter_parts.append(
            f"[{cur}]drawtext=text='{cta_safe}'"
            f":fontfile=C\\\\:/Windows/Fonts/arialbd.ttf:fontsize=38:fontcolor=0xAAFFAA"
            f":x='(w-text_w)/2'"
            f":y='{sep_y + 30}+(180-text_h)/2'"
            f":borderw=2:bordercolor=black@0.8"
            f":shadowx=2:shadowy=3:shadowcolor=black@0.6"
            f":line_spacing=-4"
            f":enable='{card_enable}'[step6]"
        )
        cur = "step6"

    filter_complex = ";".join(filter_parts)

    has_tts = tts_path and os.path.exists(tts_path)
    cmd = [
        "-stream_loop", "-1", "-t", str(outro_dur), "-i", outro_bg,
    ]
    if has_tts:
        cmd += ["-i", tts_path]
    else:
        cmd += ["-t", str(outro_dur), "-f", "lavfi",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

    cmd += [
        "-filter_complex", filter_complex,
        "-map", f"[{cur}]",
        "-map", "1:a",
        "-c:a", "aac", "-b:a", "192k",
        "-af", f"afade=t=in:d=0.5,afade=t=out:st={max(outro_dur-0.5,0):.3f}:d=0.5",
        "-t", str(outro_dur),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        outro_out,
    ]
    ok = _run_ffmpeg(cmd, label="OUTRO_COMPOSE")
    return outro_out if ok and os.path.exists(outro_out) else None



# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — Main render entry point
# ═══════════════════════════════════════════════════════════════════════════════

def render_short_ffmpeg(
    video_path: str,
    spec: dict,
    index: int,
    infobox_1:         Optional[str] = None,
    infobox_2:         Optional[str] = None,
    channel_name:      Optional[str] = None,
    theme_color:       str           = "#FF0000",
    tts_path:          Optional[str] = None,
    end_hook:          Optional[str] = None,
    cta_question:      Optional[str] = None,
    voice_id:          str           = "af_sky",
    hook_text:         Optional[str] = None,
    technical_insight: Optional[str] = None,
    judgment:          Optional[str] = None,
    sector_name:       str           = "default",
    original_handle:   Optional[str] = None,
    font_style:        str           = "Impact",
    accent_color_1:    str           = "#1E90FF",
    accent_color_2:    str           = "#FF6D00",
    sfx_set:           str           = "default",
    accent_color_3:    str           = "#39FF14",
    annotations:       Optional[list] = None,
    # Internal — injected by wrapper in legacy_runner
    _transcribe_fn=None,
    _pick_bgm_fn=None,
    _pick_sfx_fn=None,
    _shorts_dir: str = None,
    _tts_dir:    str = None,
) -> tuple:
    """
    Pure-FFmpeg + NVENC replacement for generate_vertical_short().
    Same signature, same return value: (output_path, render_elapsed_seconds).
    """
    t0 = time.time()
    shorts_dir = _shorts_dir or SHORTS_DIR
    tts_dir    = _tts_dir    or TTS_DIR
    os.makedirs(shorts_dir, exist_ok=True)

    start    = float(spec["start"])
    end      = float(spec["end"])
    clip_w   = OUTPUT_W
    clip_h   = OUTPUT_H
    fps      = OUTPUT_FPS

    tmp_dir  = tempfile.mkdtemp(prefix="ffr_")
    try:
        # ── Probe safe_end ─────────────────────────────────────────────────────
        file_dur = _probe_duration(video_path)
        safe_end = min(end, file_dur - 0.2) if file_dur > 0 else end
        clip_dur = safe_end - start
        print(f"[FFMPEG] Clip: {start:.2f}s → {safe_end:.2f}s  ({clip_dur:.2f}s)")

        # ── 1. Extract audio for Whisper ───────────────────────────────────────
        tmp_audio = os.path.join(tmp_dir, "whisper_in.mp3")
        _extract_audio_for_whisper(video_path, start, safe_end, tmp_audio)

        # ── 2. Whisper karaoke transcription ────────────────────────────────────
        word_segs = []
        ass_path  = None
        if _transcribe_fn and os.path.exists(tmp_audio):
            try:
                word_segs = _transcribe_fn(tmp_audio)
                print(f"[FFMPEG] Whisper: {len(word_segs)} word segments")
            except Exception as e:
                print(f"[FFMPEG] Whisper failed: {e}")
        if word_segs:
            ass_path = os.path.join(tmp_dir, "karaoke.ass")
            make_ass_file(word_segs, ass_path, clip_dur,
                          font_name=font_style, font_size=48)

        # ── 3. Pre-render PNG overlays ─────────────────────────────────────────
        r1,g1,b1 = _hex_to_rgb(accent_color_1)
        r2,g2,b2 = _hex_to_rgb(accent_color_2)
        r3,g3,b3 = _hex_to_rgb(accent_color_3)
        panel_w  = clip_w - 60

        pngs = {}  # label → (path, x, y, t_start, t_end, fade_in, fade_out, y_expr)

        # Title card (t=0-2s): full-screen dark overlay + hook text
        if hook_text:
            tc_png = os.path.join(tmp_dir, "title_card.png")
            try:
                from PIL import Image as _PI, ImageDraw as _PD
                tc_img  = _PI.new("RGBA", (clip_w, clip_h), (5,5,10,224))
                tc_draw = _PD.Draw(tc_img)
                bar_w   = int(clip_w * 0.65)
                bar_y   = int(clip_h * 0.57)
                r_a,g_a,b_a = _hex_to_rgb(accent_color_1)
                tc_draw.rectangle([(clip_w-bar_w)//2, bar_y,
                                   (clip_w+bar_w)//2, bar_y+5],
                                  fill=(r_a,g_a,b_a,255))
                tc_img.save(tc_png, "PNG")
                txt_png = os.path.join(tmp_dir, "title_text.png")
                _make_text_png(hook_text.strip().upper()[:60], txt_png,
                               font_name="Impact", font_size=92,
                               color=(255,255,255), stroke_width=4,
                               max_width=int(clip_w*0.82))
                pngs["title_card"] = (tc_png,  "0",   "0",   0.0, 2.0, 0.0, 0.3, None)
                pngs["title_text"] = (txt_png, "(W-w)/2", str(int(clip_h*0.36)), 0.0, 2.0, 0.0, 0.3, None)
            except Exception as e:
                print(f"[FFMPEG] Title card failed: {e}")

        # Hook/insight/ticker timing constants (used later for drawtext)
        hook_banner_start = 2.0
        hook_banner_end   = min(6.0, clip_dur - 0.2)
        ins_start = 5.0
        ins_end   = min(10.0, clip_dur - 0.2)
        t1s = min(8.2,  clip_dur * 0.41); t1e = min(13.4, max(t1s + 2.0, clip_dur * 0.67))
        t2s = min(14.0, clip_dur * 0.70); t2e = min(18.6, max(t2s + 2.0, clip_dur * 0.93))

        # Subscribe button
        btn_start = clip_dur * 0.70
        if channel_name and _HAS_PIL:
            btn_p = os.path.join(tmp_dir, "sub_btn.png")
            sz = _make_subscribe_button_png(channel_name, theme_color, btn_p)
            if sz:
                bw, bh = sz
                bx = (clip_w - bw) // 2
                by_expr = _slide_from_bottom_expr(btn_start, 1500)
                pngs["sub_btn"] = (btn_p, str(bx), by_expr,
                                   btn_start, clip_dur, 0.3, 0.0, by_expr)

        # ── 4. Build main filter_complex ───────────────────────────────────────
        fb = _FilterBuilder(fps=fps)
        # Input 0: source video (used as base)
        # Trim + background
        fb.filters.append(
            f"[0:v]trim=start={start:.4f}:end={safe_end:.4f},"
            f"setpts=PTS-STARTPTS[raw]"
        )
        fb.filters.append(
            f"[raw]scale=540:960:flags=fast_bilinear,"     # downscale FIRST
            f"gblur=sigma=12,"                              # blur on smaller frame (sigma scales with size)
            f"scale={clip_w}:{clip_h}:flags=lanczos"        # upscale back
            f":force_original_aspect_ratio=increase,"
            f"crop={clip_w}:{clip_h},"
            f"colorchannelmixer=rr=0.45:gg=0.45:bb=0.45,"
            f"setpts=PTS-STARTPTS,fps={fps}[bg]"
        )
        # Main video centered (letterboxed inside 1080x1920)
        fb.filters.append(
            f"[raw]scale={clip_w}:-2:flags=lanczos,format=yuv420p,setsar=1,"
            f"setpts=PTS-STARTPTS,fps={fps}[main_s]"
        )
        fb.filters.append(
            f"[bg][main_s]overlay=x=(W-w)/2:y=(H-h)/2:format=auto:eof_action=pass[base]"
        )
        fb._vid = "base"

        if USE_CINEMATIC_POLISH:
            fb._apply_cinematic_polish()

        # Hook face mask (t=0 to min(5,clip_dur))
        if USE_DARK_MASK_INTRO:
            hook_end = min(5.0, clip_dur)
            fb.filters.append(
                f"color=c=black@0.65:s={clip_w}x{clip_h}:r={fps},"
                f"trim=end={hook_end:.3f},setpts=PTS-STARTPTS[mask_raw]"
            )
            out = fb._new_label()
            fb.filters.append(
                f"[{fb._vid}][mask_raw]overlay=enable='lte(t,{hook_end:.3f})':eof_action=pass[{out}]"
            )
            fb._vid = out

        # Static text: branding + attribution
        if SHOW_ANALYSIS_FOOTER:
            brand = f"Analysis: {channel_name or 'Channel'}"
            fb.drawtext(brand, x="50", y="1850", fontsize=24,
                        fontcolor="white@0.85", fontfile="arial.ttf")
        if original_handle:
            handle = original_handle.lstrip("@").strip()
            if handle:
                attr = f"Original Footage: @{handle}"
                fb.drawtext(attr, x="W-tw-10", y="30", fontsize=20,
                            fontcolor="white@0.65", fontfile="arial.ttf")

        # Progress bar (pre-rendered PNG + animated scale via extra input later)
        pb_png = os.path.join(tmp_dir, "progress_bar.png")
        try:
            from PIL import Image as _PI
            r_pb, g_pb, b_pb = _hex_to_rgb(accent_color_1)
            pb_img = _PI.new("RGBA", (1080, 4), (r_pb, g_pb, b_pb, 255))
            pb_img.save(pb_png, "PNG")
        except Exception as e:
            print(f"[PROGRESS] PNG gen failed: {e}")
            pb_png = None

        # ASS subtitles
        if ass_path and os.path.exists(ass_path):
            # Golden pattern 8a: escape Windows drive colon with \\ (proven by incremental test)
            fwd = ass_path.replace("\\", "/")   # forward slashes: c:/Users/...
            safe_ass = fwd[0] + "\\\\:" + fwd[2:]  # c\\:/Users/...  (file sees c\:/path)
            out = fb._new_label()
            fb.filters.append(
                f"[{fb._vid}]subtitles={safe_ass}[{out}]"
            )
            fb._vid = out

        # ── Panel+drawtext overlays: hook, insight, ticker1, ticker2 ──────────
        if hook_text and hook_banner_end > hook_banner_start:
            hook_parts = _panel_with_text(
                text=hook_text.strip().upper()[:60],
                bg_y=160, bg_w=940, bg_h_min=140,
                fontsize=58,
                accent_color="0xFF6B00",
                bg_opacity=0.82,
                start=hook_banner_start, end=hook_banner_end,
                fade_in=0.4, fade_out=0.4,
                max_chars=20,
            )
            for part in hook_parts:
                out_lbl = fb._new_label()
                fb.filters.append(f"[{fb._vid}]{part}[{out_lbl}]")
                fb._vid = out_lbl

        if technical_insight and ins_end > ins_start:
            ins_parts = _panel_with_text(
                text=technical_insight.strip()[:160],
                bg_y=320, bg_w=1000, bg_h_min=220,
                fontsize=44,
                accent_color="0x1DB954",
                bg_opacity=0.82,
                start=ins_start, end=ins_end,
                fade_in=0.4, fade_out=0.4,
                max_chars=28,
            )
            for part in ins_parts:
                out_lbl = fb._new_label()
                fb.filters.append(f"[{fb._vid}]{part}[{out_lbl}]")
                fb._vid = out_lbl

        if infobox_1 and t1e > t1s and t1e <= clip_dur:
            t1_parts = _panel_with_text(
                text=infobox_1.strip().upper()[:50],
                bg_y=100, bg_w=820, bg_h_min=100,
                fontsize=40,
                accent_color="0xFFD700",
                bg_opacity=0.85,
                text_color="0xFFFFFF",
                start=t1s, end=t1e,
                fade_in=0.3, fade_out=0.3,
                max_chars=26,
            )
            for part in t1_parts:
                out_lbl = fb._new_label()
                fb.filters.append(f"[{fb._vid}]{part}[{out_lbl}]")
                fb._vid = out_lbl

        if infobox_2 and t2e > t2s and t2e <= clip_dur:
            t2_parts = _panel_with_text(
                text=infobox_2.strip().upper()[:50],
                bg_y=100, bg_w=820, bg_h_min=100,
                fontsize=40,
                accent_color="0xFF3366",
                bg_opacity=0.85,
                text_color="0xFFFFFF",
                start=t2s, end=t2e,
                fade_in=0.3, fade_out=0.3,
                max_chars=26,
            )
            for part in t2_parts:
                out_lbl = fb._new_label()
                fb.filters.append(f"[{fb._vid}]{part}[{out_lbl}]")
                fb._vid = out_lbl

        # PNG overlays
        png_input_start = 1  # input 0 is the video
        png_inputs_list = []
        for key, val in pngs.items():
            path, x, y, ts, te, fi, fo, y_animated = val
            if not os.path.exists(path):
                continue
            idx = png_input_start + len(png_inputs_list)
            png_inputs_list.append(path)
            dur = max(te - ts, 0.1)
            tmp_lbl = f"png_{key}"
            chain = []
            if fi > 0:
                chain.append(f"fade=t=in:st=0:d={fi:.3f}:alpha=1")
            if fo > 0:
                chain.append(f"fade=t=out:st={max(dur-fo,0):.3f}:d={fo:.3f}:alpha=1")
            chain.append(f"setpts=PTS+{ts:.4f}/TB")
            fb.filters.append(
                f"[{idx}:v]{','.join(chain)}[{tmp_lbl}]"
            )
            enable = f"between(t,{ts:.4f},{te:.4f})"
            out = fb._new_label()
            # Golden pattern: wrap x, y, and enable in single quotes
            # — protects commas inside min/max/if easing expressions from being
            #   misread as filter-chain separators when writing to a filter file.
            x_q = f"'{x}'"
            y_q = f"'{y}'"
            fb.filters.append(
                f"[{fb._vid}][{tmp_lbl}]overlay=x={x_q}:y={y_q}:enable='{enable}':eof_action=pass[{out}]"
            )
            fb._vid = out

        # Apply progress bar overlay using scale=eval=frame
        if pb_png and os.path.exists(pb_png):
            idx = png_input_start + len(png_inputs_list)
            png_inputs_list.append(pb_png)
            out = fb._new_label()
            fb.filters.append(
                f"[{idx}:v]scale=w='max(2,1080*t/{clip_dur:.4f})':h=4:eval=frame[pb_scaled]"
            )
            fb.filters.append(
                f"[{fb._vid}][pb_scaled]overlay=x=0:y=0:eof_action=pass[{out}]"
            )
            fb._vid = out

        main_vid_label = fb._vid

        # ── 5. Audio: original + bg music + sfx ───────────────────────────────
        audio_inputs  = []   # list of (ffmpeg_input_flags, filter_label)
        audio_filters = []

        # Original audio mathematically synced to exact clip_dur!
        # We generate a base track of perfect silence at the exact clip_dur length.
        # Then we mix the original audio on top. If the original audio ends early 
        # (e.g. at 19.8s), amix seamlessly continues with the silence until 20.0s.
        audio_filters.append(
            f"anullsrc=r=48000:cl=stereo,atrim=end={clip_dur:.3f},asetpts=PTS-STARTPTS[base_silence];"
            f"[0:a]atrim=start={start:.4f}:end={safe_end:.4f},asetpts=PTS-STARTPTS,"
            f"afade=t=out:st={max(clip_dur-1.0,0):.3f}:d=1.0[orig_a_raw];"
            f"[base_silence][orig_a_raw]amix=inputs=2:duration=first:dropout_transition=0[orig_a]"
        )
        mix_labels = ["orig_a"]

        # BG music
        bgm_path = _pick_bgm_fn(sector_name) if _pick_bgm_fn else None
        bgm_input_idx = None
        if bgm_path and os.path.exists(bgm_path):
            bgm_idx = 1 + len(png_inputs_list) + len(audio_inputs)
            audio_inputs.append(["-i", bgm_path])
            bgm_input_idx = bgm_idx
            audio_filters.append(
                f"[{bgm_idx}:a]"
                f"aloop=loop=-1:size=2147483647,"
                f"atrim=end={clip_dur:.4f},"
                f"asetpts=PTS-STARTPTS,"
                f"volume={BG_MUSIC_GLOBAL_VOL}[bgm_a]"
            )
            mix_labels.append("bgm_a")

        # SFX at outro boundary
        sfx_path = _pick_sfx_fn(sfx_set) if _pick_sfx_fn else None
        if sfx_path and os.path.exists(sfx_path):
            sfx_fire = max(0.0, clip_dur - 0.5)
            sfx_idx  = 1 + len(png_inputs_list) + len(audio_inputs)
            audio_inputs.append(["-i", sfx_path])
            audio_filters.append(
                f"[{sfx_idx}:a]atrim=end=3.0,volume=0.55,"
                f"adelay={int(sfx_fire*1000)}:all=1[sfx_a]"
            )
            mix_labels.append("sfx_a")

        n_mix = len(mix_labels)
        if n_mix > 1:
            mix_in = "".join(f"[{lb}]" for lb in mix_labels)
            audio_filters.append(
                f"{mix_in}amix=inputs={n_mix}:duration=first:normalize=0[main_audio]"
            )
        else:
            audio_filters.append(f"[{mix_labels[0]}]anull[main_audio]")

        # ── 6. Outro segment ──────────────────────────────────────────────────
        outro_mp4   = None
        outro_dur   = 0.0
        if cta_question:
            try:
                # Calculate outro_dur based on TTS
                if tts_path and os.path.exists(tts_path):
                    tts_duration = _probe_audio_duration(tts_path)
                    outro_dur_calc = max(5.0, tts_duration + 0.8)
                    print(f"[OUTRO] TTS={tts_duration:.2f}s → outro_dur={outro_dur_calc:.2f}s")
                else:
                    outro_dur_calc = 5.0
                    
                outro_mp4 = _render_outro_ffmpeg(
                    video_path, clip_dur + start, outro_dur_calc,
                    clip_w, clip_h, judgment or "", cta_question,
                    tts_path, accent_color_1, accent_color_3,
                    font_style, tmp_dir,
                )
                if outro_mp4 and os.path.exists(outro_mp4):
                    outro_dur = _probe_duration(outro_mp4)
                    print(f"[FFMPEG] Outro: {outro_dur:.2f}s")
            except Exception as e:
                print(f"[FFMPEG] Outro failed: {e}")
                outro_mp4 = None

        # ── 7. Final encode ────────────────────────────────────────────────────
        out_name = os.path.join(shorts_dir,
                                f"short_{index}_{uuid.uuid4().hex[:6]}.mp4")
        enc = _encode_flags()

        # Build final ffmpeg command with debug logging
        cmd = ["-loglevel", "debug", "-i", video_path]   # input 0
        for p in png_inputs_list:
            # Restore -loop 1 and -t limits at the demuxer level
            cmd += ["-loop", "1", "-t", str(round(clip_dur, 3)), "-i", p]
        for ai in audio_inputs:
            cmd += ai

        total_dur = clip_dur + outro_dur
        filter_parts = fb.filters + audio_filters

        if outro_mp4:
            outro_a_idx = 1 + len(png_inputs_list) + len(audio_inputs)
            cmd += ["-i", outro_mp4]
            filter_parts += [
                # Normalize main video: yuv420p, fixed SAR, 60fps, unified timebase
                f"[{main_vid_label}]format=yuv420p,setsar=1,fps={fps},settb=AVTB,setpts=PTS-STARTPTS[mv_norm]",
                # Normalize outro video the same way
                f"[{outro_a_idx}:v]format=yuv420p,setsar=1,fps={fps},settb=AVTB,setpts=PTS-STARTPTS[ov_norm]",
                # Video-only concat! Completely bypasses audio sample-rate/EOF deadlocks.
                f"[mv_norm][ov_norm]concat=n=2:v=1:a=0[fv]",
                # Trim main audio to EXACTLY clip_dur with a fade-out at the end
                f"[main_audio]aresample=48000:async=1:first_pts=0,asetpts=PTS-STARTPTS,"
                f"atrim=end={clip_dur:.3f},afade=t=out:st={max(clip_dur-0.4, 0):.3f}:d=0.4[ma_clean]",
                # Outro audio (TTS): reset its timestamps, fade in slightly
                f"[{outro_a_idx}:a]aresample=48000:async=1:first_pts=0,asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d=0.3[oa_clean]",
                # CONCAT them — main ends, outro starts. NO overlap.
                f"[ma_clean][oa_clean]concat=n=2:v=0:a=1[fa]",
            ]
            vmap = "[fv]"
            amap = "[fa]"
        else:
            vmap = f"[{main_vid_label}]"
            amap = "[main_audio]"

        # Build a clean single-line filter string (no newlines — new FFmpeg rejects them)
        filter_str = ";".join(
            part.strip().rstrip(";")
            for part in filter_parts
            if part.strip()
        ).replace(";;", ";")

        # Write filter script and use -/filter_complex (replaces deprecated -filter_complex_script)
        # No further sanitisation needed: single quotes around enable=between() and x=/y= expressions
        # are the correct golden pattern for filter-file mode (proven by incremental_test.py stages 6-9).
        filter_file = os.path.join(tmp_dir, "filter_script.txt")
        with open(filter_file, "w", encoding="utf-8") as _ff:
            _ff.write(filter_str)
        print(f"[FFMPEG] filter_complex ({len(filter_str)} chars): {filter_str[:600]}")
        print(f"[FFMPEG] filter file: {filter_file}")

        cmd += [
            "-/filter_complex", filter_file,
            "-map", vmap,
            "-map", amap,
            "-r", str(fps),
            "-t", str(round(total_dur, 3)),
        ] + enc + [out_name]

        ok = _run_ffmpeg(cmd, label="RENDER")

        elapsed = time.time() - t0
        if ok and os.path.exists(out_name):
            print(f"[FFMPEG] ✓ Done in {elapsed:.1f}s ({elapsed/60:.1f} min): {os.path.basename(out_name)}")
            return out_name, elapsed
        raise RuntimeError(f"[FFMPEG] Encode failed. Check logs above.")

    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
