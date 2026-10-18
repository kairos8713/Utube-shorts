import os
import time
import subprocess
import re

try:
    import numpy as np
except ImportError:
    np = None

try:
    import librosa
except ImportError:
    librosa = None


def extract_audio_dynamics(audio_path: str, word_segments: list) -> list:
    """
    Extract volume, pitch, and speech rate for each transcribed word.
    Flags words as 'emphasized' if they are loud and (high pitch OR slow speech rate).
    """
    if librosa is None or np is None:
        print("[MULTIMODAL] librosa or numpy not installed. Returning basic word segments.")
        return word_segments

    if not os.path.exists(audio_path):
        print(f"[MULTIMODAL] Audio file not found: {audio_path}")
        return word_segments

    t0 = time.time()
    try:
        y, sr = librosa.load(audio_path, sr=22050)
        rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
        # Using pyin for better pitch tracking, falling back to yin if too slow, but yin is requested
        # For speed, using yin over pyin
        pitch = librosa.yin(y=y, fmin=80, fmax=400, sr=sr)
        
        # Calculate overall statistics
        valid_pitch = pitch[pitch > 0]
        overall_mean_rms = np.mean(rms) if len(rms) > 0 else 0
        overall_std_rms = np.std(rms) if len(rms) > 0 else 0
        overall_mean_pitch = np.mean(valid_pitch) if len(valid_pitch) > 0 else 0
        overall_std_pitch = np.std(valid_pitch) if len(valid_pitch) > 0 else 0

        # Precompute speech rates for all words
        rates = []
        for w in word_segments:
            start, end = w.get("start", 0), w.get("end", 0)
            word_str = w.get("word", "").strip()
            dur = end - start
            if dur > 0:
                rates.append(len(word_str) / dur)
        
        overall_mean_rate = np.mean(rates) if rates else 0
        overall_std_rate = np.std(rates) if rates else 0

        enriched = []
        for w in word_segments:
            start, end = w.get("start", 0), w.get("end", 0)
            word_str = w.get("word", "")
            
            # Convert time to frames
            start_frame = librosa.time_to_frames(start, sr=sr, hop_length=512)
            end_frame = librosa.time_to_frames(end, sr=sr, hop_length=512)
            
            # Extract slices
            if start_frame < end_frame and start_frame < len(rms):
                word_rms = np.mean(rms[start_frame:end_frame])
                word_pitch_slice = pitch[start_frame:end_frame]
                word_pitch_slice = word_pitch_slice[word_pitch_slice > 0]
                word_pitch = np.mean(word_pitch_slice) if len(word_pitch_slice) > 0 else 0
            else:
                word_rms = 0
                word_pitch = 0

            # Classify volume
            if word_rms > (overall_mean_rms + 1.0 * overall_std_rms):
                vol_class = "loud"
            elif word_rms < (overall_mean_rms - 0.5 * overall_std_rms):
                vol_class = "quiet"
            else:
                vol_class = "normal"
                
            # Classify pitch
            if word_pitch > (overall_mean_pitch + 0.5 * overall_std_pitch):
                pitch_class = "high"
            elif word_pitch < (overall_mean_pitch - 0.5 * overall_std_pitch) and word_pitch > 0:
                pitch_class = "low"
            else:
                pitch_class = "mid"
                
            # Classify speech rate
            dur = end - start
            rate = len(word_str.strip()) / dur if dur > 0 else 0
            if rate > (overall_mean_rate + 1.0 * overall_std_rate):
                rate_class = "fast"
            elif rate < (overall_mean_rate - 1.0 * overall_std_rate) and rate > 0:
                rate_class = "slow"
            else:
                rate_class = "normal"

            emphasized = vol_class == "loud" and (pitch_class == "high" or rate_class == "slow")

            new_w = w.copy()
            new_w.update({
                "volume": vol_class,
                "pitch": pitch_class,
                "speech_rate": rate_class,
                "emphasized": emphasized
            })
            enriched.append(new_w)
            
        print(f"[MULTIMODAL] extract_audio_dynamics took {time.time()-t0:.2f}s")
        return enriched

    except Exception as e:
        print(f"[MULTIMODAL] Error in audio dynamics: {e}")
        return word_segments


def extract_energy_map(audio_path: str, bucket_seconds: float = 1.0) -> list:
    """
    Bucket the audio into N-second windows and compute normalized RMS energy per bucket.
    """
    if librosa is None or np is None:
        return []

    if not os.path.exists(audio_path):
        return []

    t0 = time.time()
    try:
        y, sr = librosa.load(audio_path, sr=22050)
        rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
        
        frames_per_bucket = librosa.time_to_frames(bucket_seconds, sr=sr, hop_length=512)
        if frames_per_bucket == 0:
            frames_per_bucket = 1
            
        buckets = []
        max_energy = 0
        
        for i in range(0, len(rms), frames_per_bucket):
            slice_rms = rms[i:i+frames_per_bucket]
            mean_rms = np.mean(slice_rms) if len(slice_rms) > 0 else 0
            max_energy = max(max_energy, mean_rms)
            
            start_t = librosa.frames_to_time(i, sr=sr, hop_length=512)
            end_t = librosa.frames_to_time(i+len(slice_rms), sr=sr, hop_length=512)
            buckets.append({
                "start": float(start_t),
                "end": float(end_t),
                "raw_energy": float(mean_rms)
            })

        # Normalize and classify
        for b in buckets:
            norm = (b["raw_energy"] / max_energy) if max_energy > 0 else 0
            b["energy"] = round(norm, 2)
            del b["raw_energy"]
            
            if norm < 0.4:
                b["level"] = "calm"
            elif norm < 0.7:
                b["level"] = "building"
            else:
                b["level"] = "peak"
                
        # Mark cooldowns (if previous was peak and current is lower)
        for i in range(1, len(buckets)):
            if buckets[i-1]["level"] == "peak" and buckets[i]["energy"] < 0.7:
                buckets[i]["level"] = "cooldown"

        print(f"[MULTIMODAL] extract_energy_map took {time.time()-t0:.2f}s")
        return buckets

    except Exception as e:
        print(f"[MULTIMODAL] Error in energy map: {e}")
        return []


def detect_pauses(word_segments: list, min_pause: float = 0.4) -> list:
    """
    Detect gaps between consecutive words that exceed min_pause threshold.
    """
    pauses = []
    for i in range(len(word_segments) - 1):
        end_current = word_segments[i].get("end", 0)
        start_next = word_segments[i+1].get("start", 0)
        gap = start_next - end_current
        
        if gap >= min_pause:
            context = "scene break" if gap > 0.6 else "dramatic pause"
            pauses.append({
                "start": round(end_current, 2),
                "duration": round(gap, 2),
                "context": context
            })
            
    return pauses


def detect_scene_changes(video_path: str, start: float, end: float, threshold: float = 0.3) -> list:
    """
    Detect scene cuts using ffmpeg's select filter.
    """
    if not os.path.exists(video_path):
        return []

    t0 = time.time()
    cmd = [
        "ffmpeg", "-ss", str(start), "-to", str(end), "-i", video_path,
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-an", "-f", "null", "-"
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        output = proc.stderr
        
        # Parse output for pts_time
        pattern = re.compile(r"pts_time:([0-9\.]+)")
        matches = pattern.findall(output)
        
        cuts = []
        for m in matches:
            cuts.append({
                "time": float(m),
                "type": "cut"
            })
            
        print(f"[MULTIMODAL] detect_scene_changes took {time.time()-t0:.2f}s (found {len(cuts)})")
        return cuts

    except Exception as e:
        print(f"[MULTIMODAL] Error in scene detection: {e}")
        return []


def build_context_bundle(video_path: str, clip_start: float, clip_end: float, audio_path: str, word_segments: list) -> dict:
    """
    Master function to orchestrate multimodal extraction.
    """
    enriched_transcript = extract_audio_dynamics(audio_path, word_segments)
    energy_map = extract_energy_map(audio_path, bucket_seconds=1.0)
    pauses = detect_pauses(word_segments, min_pause=0.4)
    scene_changes = detect_scene_changes(video_path, clip_start, clip_end, threshold=0.3)
    
    # Find audio peak time
    audio_peak_time = 0
    max_energy = -1
    for b in energy_map:
        if b["energy"] > max_energy:
            max_energy = b["energy"]
            audio_peak_time = (b["start"] + b["end"]) / 2.0

    bundle = {
        "clip_duration": clip_end - clip_start,
        "enriched_transcript": enriched_transcript,
        "energy_map": energy_map,
        "pauses": pauses,
        "scene_changes": scene_changes,
        "audio_peak_time": round(audio_peak_time, 2),
        "total_pauses": len(pauses),
        "total_scene_changes": len(scene_changes)
    }
    
    return bundle


def format_for_qwen(bundle: dict) -> str:
    """
    Format the context bundle into clean text for Qwen prompts.
    """
    lines = []
    
    # Transcript
    lines.append("ENRICHED TRANSCRIPT (with audio dynamics):")
    for w in bundle.get("enriched_transcript", []):
        start = w.get("start", 0)
        end = w.get("end", 0)
        word = w.get("word", "").strip()
        
        # We might have basic words if librosa failed
        vol = w.get("volume")
        pitch = w.get("pitch")
        rate = w.get("speech_rate")
        emph = w.get("emphasized")
        
        if vol:
            tags = []
            if vol == "loud":
                tags.append("LOUD")
            elif vol == "quiet":
                tags.append("quiet")
            else:
                tags.append("normal")
                
            if pitch == "high":
                tags.append("HIGH")
            elif pitch == "low":
                tags.append("low")
                
            if rate == "fast":
                tags.append("fast")
            elif rate == "slow":
                tags.append("slow")
                
            if emph:
                tags.append("EMPHASIZED")
                
            tag_str = f"({', '.join(tags)})" if tags else "(normal)"
        else:
            tag_str = ""

        # Check if there is a pause right after this word
        # We can approximate by checking pauses list, but for simplicity we just print the word
        lines.append(f"[{start:.2f}-{end:.2f}] \"{word}\" {tag_str}".strip())
    
    # Pauses interleaf
    if bundle.get("pauses"):
        lines.append("\nPAUSES (drama beats):")
        for p in bundle["pauses"]:
            lines.append(f"  t={p['start']:.2f}s ({p['duration']:.1f}s) — {p['context']}")
            
    # Energy Map
    if bundle.get("energy_map"):
        lines.append("\nAUDIO ENERGY MAP:")
        for b in bundle["energy_map"]:
            lines.append(f"[{b['start']:.0f}-{b['end']:.0f}s] energy={b['energy']:.2f} ({b['level']})")
            
    # Scene changes
    if bundle.get("scene_changes"):
        lines.append("\nSCENE CHANGES (visual cuts):")
        cuts = [f"t={c['time']:.2f}s" for c in bundle["scene_changes"]]
        lines.append(f"  {', '.join(cuts)}")
        
    # Key alignments
    lines.append("\nKEY ALIGNMENT POINTS (audio peak + scene change within 1s of each other):")
    peak = bundle.get("audio_peak_time", 0)
    
    alignments_found = False
    for c in bundle.get("scene_changes", []):
        if abs(c["time"] - peak) <= 1.0:
            lines.append(f"  t={peak:.1f}s — audio peak aligns with scene cut at {c['time']:.1f}s — CLIMACTIC MOMENT")
            alignments_found = True
            break
            
    if not alignments_found:
        lines.append(f"  Audio peak at t={peak:.1f}s, no immediately adjacent scene cuts.")

    return "\n".join(lines)


if __name__ == "__main__":
    # Test with the existing test segment
    test_video = r"c:/Users/emre8/Desktop/utube/segments/qUUfucHC7tU_part1.mp4"
    test_audio = "tmp_test_audio.mp3"
    
    print("[MULTIMODAL] Starting test...")
    
    # Extract audio
    if not os.path.exists(test_audio):
        print("[MULTIMODAL] Extracting audio...")
        subprocess.run([
            "ffmpeg", "-y", "-ss", "5", "-to", "25", "-i", test_video,
            "-vn", "-c:a", "libmp3lame", "-q:a", "4", test_audio
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
    # Mock word segments since we don't want to load Whisper just for a quick test
    mock_word_segments = [
        {"word": "I'm", "start": 0.0, "end": 0.32},
        {"word": "gonna", "start": 0.32, "end": 0.58},
        {"word": "tell", "start": 0.58, "end": 1.20},
        {"word": "you", "start": 1.20, "end": 2.10},
        {"word": "something", "start": 2.60, "end": 3.10},
        {"word": "INSANE", "start": 3.10, "end": 3.90},
    ]
    
    print("[MULTIMODAL] Building context bundle...")
    bundle = build_context_bundle(test_video, 5.0, 25.0, test_audio, mock_word_segments)
    
    print("\n" + "="*60)
    print("OUTPUT FORMAT FOR QWEN:")
    print("="*60)
    print(format_for_qwen(bundle))
    print("="*60)
