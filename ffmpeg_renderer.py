"""
ffmpeg_renderer.py  —  IG Reels FFmpeg + NVENC rendering pipeline
==================================================================
Two-phase render:
  1. render_raw_ffmpeg()     → 1080×1920 @ 24fps (bg + polish + audio, no overlays)
  2. burn_overlays_ffmpeg()  → ASS subs + progress bar at final res/fps + NVENC encode

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

# ── IG Reels Output Config ────────────────────────────────────────────────────
OUTPUT_W        = 1080          # Base render resolution (upscaled later by ESRGAN)
OUTPUT_H        = 1920          # 9:16 vertical
OUTPUT_FPS      = 24            # Base render fps (RIFE --multi 3 → 72fps post)
FINAL_W         = 2160          # After ESRGAN 2× upscale
FINAL_H         = 3840          # After ESRGAN 2× upscale
FINAL_FPS       = 72            # Target delivery fps after RIFE interpolation
ZONE2_Y_ANCHOR  = int(OUTPUT_H * 0.82)   # Subtitle zone (higher for IG)
BG_MUSIC_GLOBAL_VOL = 0.07      # Slightly lower for IG's raw aesthetic
USE_CINEMATIC_POLISH = True     # IG-tuned color grade (warmer)

REELS_DIR = os.path.abspath("reels")

# ── NVENC encode flags (final delivery — 2160×3840 @ 72fps) ───────────────────
_NVENC_FLAGS = [
    "-c:v", "h264_nvenc",
    "-preset", "p4",
    "-tune", "hq",
    "-rc", "vbr",
    "-cq", "20",          # tighter quality for IG (was 23)
    "-b:v", "12M",        # higher bitrate for 2160×3840
    "-maxrate", "18M",
    "-bufsize", "24M",
    "-profile:v", "high",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac",
    "-b:a", "192k",
]

# ── Intermediate encode flags (raw render — will be re-encoded later) ─────────
_INTERMEDIATE_FLAGS = [
    "-c:v", "libx264",
    "-preset", "fast",
    "-crf", "16",         # near-lossless intermediate
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
    tags = "{\\fad(80,100)}"  # Simple fade in/out -- no kinetic pop
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
Style: Default,Arial,62,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,4,2,2,40,40,360,1
Style: Highlight,Arial,70,&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,5,3,2,40,40,360,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogues = "\n".join(filter(None, [_word_to_dialogue(w, video_w) for w in word_segments]))
    return header + dialogues + "\n"


    words_per = 4

def make_ass_file(word_segments: List[dict], out_path: str,
                  clip_duration: float,
                  font_name: str = "Impact",
                  font_size: int = 48) -> bool:
    try:
        ass_content = _build_kinetic_ass(word_segments, video_w=OUTPUT_W, video_h=OUTPUT_H)
        print(f"[ASS] Kinetic typography: {len(word_segments)} animated words")
            
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(ass_content)
        return True
    except Exception as e:
        import traceback
        print(f"[ASS] Failed to build ASS file: {e}")
        traceback.print_exc()
        raise



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
# SECTION 7 — IG Reels two-phase render pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def render_raw_ffmpeg(
    video_path: str,
    spec: dict,
    index: int,
    sector_name:       str           = "default",
    original_handle:   Optional[str] = None,
    # Internal — injected by wrapper in legacy_runner / sector_processor
    _transcribe_fn=None,
    _pick_bgm_fn=None,
    _reels_dir: str = None,
) -> tuple:
    """
    Phase 1 of IG Reels pipeline: raw render.

    Produces:
      - raw_mp4: 1080×1920 @ 24fps, bg + cinematic polish + mixed audio
      - ass_path: ASS subtitle sidecar file (from Whisper), or None
      - clip_duration: float
      - elapsed: render time in seconds

    No text/graphic overlays burned in — those are applied AFTER
    upscale + interpolation by burn_overlays_ffmpeg().
    """
    t0 = time.time()
    reels_dir = _reels_dir or REELS_DIR
    os.makedirs(reels_dir, exist_ok=True)

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
        print(f"[FFMPEG] Clip: {start:.2f}s -> {safe_end:.2f}s  ({clip_dur:.2f}s)")

        # ── 1. Extract audio for Whisper ───────────────────────────────────────
        tmp_audio = os.path.join(tmp_dir, "whisper_in.mp3")
        _extract_audio_for_whisper(video_path, start, safe_end, tmp_audio)

        # ── 2. Whisper karaoke transcription → ASS sidecar ────────────────────
        word_segs = []
        ass_path  = None
        if _transcribe_fn and os.path.exists(tmp_audio):
            try:
                word_segs = _transcribe_fn(tmp_audio)
                print(f"[FFMPEG] Whisper: {len(word_segs)} word segments")
            except Exception as e:
                print(f"[FFMPEG] Whisper failed: {e}")
        if word_segs:
            # Save ASS sidecar alongside the output MP4 (not in tmp_dir — needed later)
            ass_path = os.path.join(reels_dir, f"subs_{index}_{uuid.uuid4().hex[:6]}.ass")
            make_ass_file(word_segs, ass_path, clip_dur,
                          font_name="Impact", font_size=48)

        # ── 3. Build main filter_complex (bg + polish ONLY) ───────────────────
        fb = _FilterBuilder(fps=fps)
        # Trim + background
        fb.filters.append(
            f"[0:v]trim=start={start:.4f}:end={safe_end:.4f},"
            f"setpts=PTS-STARTPTS[raw]"
        )
        fb.filters.append(
            f"[raw]scale=540:960:flags=fast_bilinear,"     # downscale FIRST
            f"gblur=sigma=12,"                              # blur on smaller frame
            f"scale={clip_w}:{clip_h}:flags=lanczos"        # upscale back
            f":force_original_aspect_ratio=increase,"
            f"crop={clip_w}:{clip_h},setsar=1,"
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

        # Original handle attribution (small text, top-right)
        if original_handle:
            handle = original_handle.lstrip("@").strip()
            if handle:
                attr = f"Original Footage: @{handle}"
                fb.drawtext(attr, x="W-tw-10", y="30", fontsize=20,
                            fontcolor="white@0.65", fontfile="arial.ttf")

        main_vid_label = fb._vid

        # ── 4. Audio: original + bg music ──────────────────────────────────────
        audio_inputs  = []
        audio_filters = []

        # Original audio with silence padding to exact clip_dur
        audio_filters.append(
            f"anullsrc=r=48000:cl=stereo,atrim=end={clip_dur:.3f},asetpts=PTS-STARTPTS[base_silence];"
            f"[0:a]atrim=start={start:.4f}:end={safe_end:.4f},asetpts=PTS-STARTPTS,"
            f"afade=t=out:st={max(clip_dur-1.0,0):.3f}:d=1.0[orig_a_raw];"
            f"[base_silence][orig_a_raw]amix=inputs=2:duration=first:dropout_transition=0[orig_a]"
        )
        mix_labels = ["orig_a"]

        # BG music
        bgm_path = _pick_bgm_fn(sector_name) if _pick_bgm_fn else None
        if bgm_path and os.path.exists(bgm_path):
            bgm_idx = 1 + len(audio_inputs)
            audio_inputs.append(["-i", bgm_path])
            audio_filters.append(
                f"[{bgm_idx}:a]"
                f"aloop=loop=-1:size=2147483647,"
                f"atrim=end={clip_dur:.4f},"
                f"asetpts=PTS-STARTPTS,"
                f"volume={BG_MUSIC_GLOBAL_VOL}[bgm_a]"
            )
            mix_labels.append("bgm_a")

        n_mix = len(mix_labels)
        if n_mix > 1:
            mix_in = "".join(f"[{lb}]" for lb in mix_labels)
            audio_filters.append(
                f"{mix_in}amix=inputs={n_mix}:duration=first:normalize=0[main_audio]"
            )
        else:
            audio_filters.append(f"[{mix_labels[0]}]anull[main_audio]")

        # ── 5. Intermediate encode (libx264, near-lossless) ────────────────────
        out_name = os.path.join(reels_dir,
                                f"raw_{index}_{uuid.uuid4().hex[:6]}.mp4")

        cmd = ["-loglevel", "warning", "-i", video_path]
        for ai in audio_inputs:
            cmd += ai

        filter_parts = fb.filters + audio_filters
        filter_str = ";".join(
            part.strip().rstrip(";")
            for part in filter_parts
            if part.strip()
        ).replace(";;", ";")

        filter_file = os.path.join(tmp_dir, "filter_script.txt")
        with open(filter_file, "w", encoding="utf-8") as _ff:
            _ff.write(filter_str)
        print(f"[FFMPEG] filter_complex ({len(filter_str)} chars): {filter_str[:400]}")

        cmd += [
            "-/filter_complex", filter_file,
            "-map", f"[{main_vid_label}]",
            "-map", "[main_audio]",
            "-r", str(fps),
            "-t", str(round(clip_dur, 3)),
        ] + _INTERMEDIATE_FLAGS + [out_name]

        ok = _run_ffmpeg(cmd, label="RAW_RENDER")

        elapsed = time.time() - t0
        if ok and os.path.exists(out_name):
            print(f"[FFMPEG] ✓ Raw render done in {elapsed:.1f}s: {os.path.basename(out_name)}")
            return out_name, ass_path, clip_dur, elapsed
        raise RuntimeError("[FFMPEG] Raw render failed. Check logs above.")

    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def burn_overlays_ffmpeg(
    video_path: str,
    audio_path: str,
    ass_path: Optional[str],
    clip_duration: float,
    output_path: str,
    accent_color_1: str = "#1E90FF",
) -> bool:
    """
    Phase 4 of IG Reels pipeline: overlay burn-in + final NVENC encode.

    Applied AFTER upscale (2160×3840) and interpolation (72fps).
    Burns:
      - ASS subtitles (auto-scaled from 1080×1920 PlayRes to actual resolution)
      - Progress bar (2px thin white line, top edge, animated)

    Also:
      - Re-muxes audio from the raw render (RIFE output has no audio)
      - Final encode via h264_nvenc with production quality flags
    """
    t0 = time.time()
    tmp_dir = tempfile.mkdtemp(prefix="ffr_overlay_")

    try:
        # Probe actual video dimensions (may be 2160×3840 after ESRGAN, or 1080×1920 if fallback)
        try:
            probe_cmd = [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", "-select_streams", "v:0", video_path,
            ]
            r = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=15)
            streams = json.loads(r.stdout).get("streams", [{}])
            vid_w = int(streams[0].get("width", FINAL_W))
            vid_h = int(streams[0].get("height", FINAL_H))
        except Exception:
            vid_w, vid_h = FINAL_W, FINAL_H

        print(f"[OVERLAY] Burning overlays on {vid_w}×{vid_h} video")

        # ── 1. Generate progress bar PNG ───────────────────────────────────────
        pb_png = os.path.join(tmp_dir, "progress_bar.png")
        try:
            from PIL import Image as _PI
            # Thin white bar, 60% opacity
            pb_img = _PI.new("RGBA", (vid_w, 2), (255, 255, 255, 153))
            pb_img.save(pb_png, "PNG")
        except Exception as e:
            print(f"[OVERLAY] Progress bar PNG failed: {e}")
            pb_png = None

        # ── 2. Build filter chain ──────────────────────────────────────────────
        filters = []

        # ASS subtitles — auto-scaled via PlayResX/PlayResY
        vid_label = "0:v"
        if ass_path and os.path.exists(ass_path):
            fwd = ass_path.replace("\\", "/")
            safe_ass = fwd[0] + "\\\\:" + fwd[2:]  # Windows drive colon escape
            filters.append(
                f"[0:v]subtitles={safe_ass}[v_sub]"
            )
            vid_label = "v_sub"

        # Progress bar overlay
        if pb_png and os.path.exists(pb_png):
            # Scale bar width from 2px to full width over clip_duration
            filters.append(
                f"[1:v]scale=w='max(2,{vid_w}*t/{clip_duration:.4f})':h=2:eval=frame[pb_scaled]"
            )
            src = vid_label if vid_label != "0:v" else "0:v"
            if vid_label == "0:v":
                # No subtitle filter was applied, work directly on input
                filters.append(
                    f"[0:v][pb_scaled]overlay=x=0:y=0:eof_action=pass[v_final]"
                )
            else:
                filters.append(
                    f"[{vid_label}][pb_scaled]overlay=x=0:y=0:eof_action=pass[v_final]"
                )
            vid_label = "v_final"

        # ── 3. Build FFmpeg command ────────────────────────────────────────────
        cmd = ["-loglevel", "warning"]

        # Input 0: RIFE output (video-only, or video+audio)
        cmd += ["-i", video_path]

        # Input 1: progress bar PNG (if available)
        if pb_png and os.path.exists(pb_png):
            cmd += ["-loop", "1", "-t", str(round(clip_duration, 3)), "-i", pb_png]

        # Input 2: audio source (raw render MP4)
        cmd += ["-i", audio_path]

        # Audio input index depends on whether pb_png was added
        audio_input_idx = 2 if (pb_png and os.path.exists(pb_png)) else 1

        enc = _encode_flags()

        if filters:
            filter_str = ";".join(filters)
            filter_file = os.path.join(tmp_dir, "overlay_filter.txt")
            with open(filter_file, "w", encoding="utf-8") as _ff:
                _ff.write(filter_str)
            print(f"[OVERLAY] filter: {filter_str[:300]}")

            cmd += [
                "-/filter_complex", filter_file,
                "-map", f"[{vid_label}]",
                "-map", f"{audio_input_idx}:a:0",
            ]
        else:
            # No overlays — just re-encode video + mux audio
            cmd += [
                "-map", "0:v:0",
                "-map", f"{audio_input_idx}:a:0",
            ]

        cmd += [
            "-t", str(round(clip_duration, 3)),
        ] + enc + [output_path]

        ok = _run_ffmpeg(cmd, label="OVERLAY_ENCODE")

        elapsed = time.time() - t0
        if ok and os.path.exists(output_path):
            print(f"[OVERLAY] ✓ Final encode done in {elapsed:.1f}s: {os.path.basename(output_path)}")
            return True

        print("[OVERLAY] ✗ Final encode failed")
        return False

    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
