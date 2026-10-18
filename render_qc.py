"""
render_qc.py — quality gate before a reel may enter the upload queue.

Hard failures (video is NOT queued, reason logged):
  * not 1080×1920 / not ~60 fps / not H.264 / no AAC 48 kHz audio
  * duration outside expected ± tolerance
  * file size out of range, bitrate > 25 Mbps (IG limit)
  * moov atom not at the front (+faststart missing)
  * black frames at the very start, or > 1 s black in total
  * silence ≥ 4 s, or no audible audio at all
Warnings only: bitrate far from 8 Mbps, integrated loudness outside -17..-11 LUFS.
"""
import json
import os
import re
import struct
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Optional

from log_setup import get_logger

log = get_logger("render_qc")

EXPECT_W, EXPECT_H, EXPECT_FPS = 1080, 1920, 60.0
FPS_TOL = 0.6
DURATION_TOL_S = 0.6
MIN_BYTES, MAX_BYTES = 300_000, 100_000_000
MAX_BITRATE = 25_000_000
MAX_SILENCE_S = 4.0
MAX_BLACK_TOTAL_S = 1.0


@dataclass
class QCResult:
    passed: bool
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


def _fps(rate: str) -> float:
    try:
        num, den = rate.split("/")
        return float(num) / float(den) if float(den) else 0.0
    except Exception:
        return 0.0


def moov_before_mdat(path: str) -> bool:
    """Walk top-level MP4 atoms; +faststart puts 'moov' before 'mdat'."""
    with open(path, "rb") as f:
        offset, size_total = 0, os.path.getsize(path)
        while offset < size_total:
            f.seek(offset)
            hdr = f.read(8)
            if len(hdr) < 8:
                return False
            size, kind = struct.unpack(">I4s", hdr)
            if size == 1:
                size = struct.unpack(">Q", f.read(8))[0]
            elif size == 0:
                size = size_total - offset
            if kind == b"moov":
                return True
            if kind == b"mdat":
                return False
            if size < 8:
                return False
            offset += size
    return False


def analyse_av(path: str) -> dict:
    """One ffmpeg pass: blackdetect + silencedetect + ebur128."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
           "-vf", "blackdetect=d=0.1:pix_th=0.10",
           "-af", "silencedetect=noise=-50dB:d=1.0,ebur128=peak=true",
           "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
    err = r.stderr or ""
    blacks = [(float(a), float(b)) for a, b in
              re.findall(r"black_start:([\d.]+)\s+black_end:([\d.]+)", err)]
    silences = [float(x) for x in re.findall(r"silence_duration:\s*([\d.]+)", err)]
    open_silence = re.findall(r"silence_start:\s*([\d.]+)", err)
    m = re.search(r"Integrated loudness:\s*I:\s*(-?[\d.]+)\s*LUFS", err)
    integrated = float(m.group(1)) if m else None
    tp = re.search(r"True peak:\s*Peak:\s*(-?[\d.]+|-inf)\s*dBFS", err)
    return {
        "black_segments": blacks,
        "silence_durations": silences,
        "silence_starts": [float(x) for x in open_silence],
        "integrated_lufs": integrated,
        "true_peak_dbfs": float(tp.group(1)) if tp and tp.group(1) != "-inf" else None,
    }


def check_video(path: str, expected_duration: Optional[float] = None) -> QCResult:
    res = QCResult(passed=True)
    if not os.path.exists(path):
        return QCResult(False, ["file missing"])
    size = os.path.getsize(path)
    res.metrics["size_bytes"] = size
    if not (MIN_BYTES <= size <= MAX_BYTES):
        res.reasons.append(f"file size {size} outside [{MIN_BYTES}, {MAX_BYTES}]")

    try:
        r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format",
                            "-show_streams", path], capture_output=True, text=True, timeout=60)
        info = json.loads(r.stdout or "{}")
    except Exception as e:
        return QCResult(False, [f"ffprobe failed: {e}"])

    vs = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    aus = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    fmt = info.get("format", {})
    if not vs:
        res.reasons.append("no video stream")
    else:
        v = vs[0]
        fps = _fps(v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1")
        res.metrics.update(width=v.get("width"), height=v.get("height"), fps=round(fps, 3),
                           vcodec=v.get("codec_name"), pix_fmt=v.get("pix_fmt"),
                           v_bitrate=int(v.get("bit_rate") or 0))
        if (v.get("width"), v.get("height")) != (EXPECT_W, EXPECT_H):
            res.reasons.append(f"resolution {v.get('width')}x{v.get('height')} != {EXPECT_W}x{EXPECT_H}")
        if abs(fps - EXPECT_FPS) > FPS_TOL:
            res.reasons.append(f"fps {fps:.2f} != {EXPECT_FPS}")
        if v.get("codec_name") != "h264":
            res.reasons.append(f"video codec {v.get('codec_name')} != h264")
        if v.get("pix_fmt") != "yuv420p":
            res.warnings.append(f"pix_fmt {v.get('pix_fmt')}")
    if not aus:
        res.reasons.append("no audio stream")
    else:
        a = aus[0]
        res.metrics.update(acodec=a.get("codec_name"), sample_rate=int(a.get("sample_rate") or 0),
                           channels=a.get("channels"))
        if a.get("codec_name") != "aac":
            res.reasons.append(f"audio codec {a.get('codec_name')} != aac")
        if int(a.get("sample_rate") or 0) != 48000:
            res.reasons.append(f"audio sample rate {a.get('sample_rate')} != 48000")

    duration = float(fmt.get("duration") or 0)
    bitrate = int(fmt.get("bit_rate") or 0)
    res.metrics.update(duration=duration, bitrate=bitrate)
    if expected_duration is not None and abs(duration - expected_duration) > DURATION_TOL_S:
        res.reasons.append(f"duration {duration:.2f}s != expected {expected_duration:.2f}s ± {DURATION_TOL_S}")
    if bitrate > MAX_BITRATE:
        res.reasons.append(f"bitrate {bitrate} > IG max {MAX_BITRATE}")
    elif bitrate and not (4_000_000 <= bitrate <= 12_000_000):
        res.warnings.append(f"bitrate {bitrate / 1e6:.1f} Mbps far from 8 Mbps target")

    try:
        fast = moov_before_mdat(path)
    except Exception:
        fast = False
    res.metrics["faststart"] = fast
    if not fast:
        res.reasons.append("moov atom not at front (+faststart missing)")

    if vs and aus:  # A/V analysis needs both streams
        av = analyse_av(path)
        res.metrics.update(av)
        blacks = av["black_segments"]
        if blacks and blacks[0][0] <= 0.05:
            res.reasons.append(f"black/darkened first frames ({blacks[0][0]:.2f}-{blacks[0][1]:.2f}s)")
        black_total = sum(b - a for a, b in blacks)
        if black_total > MAX_BLACK_TOTAL_S:
            res.reasons.append(f"{black_total:.2f}s of black frames")
        longest = max(av["silence_durations"] or [0.0])
        if longest >= MAX_SILENCE_S:
            res.reasons.append(f"silence of {longest:.1f}s")
        # an unterminated silence that starts at 0 means the whole file is silent
        if av["silence_starts"] and len(av["silence_starts"]) > len(av["silence_durations"]) \
                and av["silence_starts"][-1] <= 0.05:
            res.reasons.append("audio is silent")
        lufs = av["integrated_lufs"]
        if lufs is None or lufs < -40:
            res.reasons.append(f"no audible audio (integrated {lufs} LUFS)")
        elif not (-17.0 <= lufs <= -11.0):
            res.warnings.append(f"integrated loudness {lufs} LUFS outside -17..-11")

    res.passed = not res.reasons
    (log.info if res.passed else log.warning)("QC %s %s reasons=%s warnings=%s",
                                              "PASS" if res.passed else "FAIL",
                                              os.path.basename(path), res.reasons, res.warnings)
    return res
