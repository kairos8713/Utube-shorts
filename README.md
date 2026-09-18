# 🎬 YouTube Shorts Automation Pipeline

*An automated, high-throughput media generation and video processing pipeline engineered to convert long-form audio/video content into vertical Shorts format (9:16) with dynamic subtitles.*

![Python](https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white)
![FFmpeg](https://img.shields.io/badge/FFmpeg-007808?style=flat-square&logo=ffmpeg&logoColor=white)
![OpenAI Whisper](https://img.shields.io/badge/OpenAI_Whisper-412991?style=flat-square&logo=openai&logoColor=white)
![MoviePy](https://img.shields.io/badge/MoviePy-FF4B4B?style=flat-square)
![Pillow](https://img.shields.io/badge/Pillow-11557C?style=flat-square)

---

## 🛠️ Development Approach

Built solo, with AI-assisted tooling used for rapid prototyping, subtitle alignment logic, and media processing pipeline optimization. All architecture decisions, video processing pipelines, and production rendering flows were designed and validated by me.

---

## 📌 Engineering Logic & System Architecture

This pipeline is designed for batch-processing and automated video composition, eliminating manual timeline editing for short-form platforms (YouTube Shorts, TikTok, Instagram Reels).

### Core Pipeline Components

- **Frame Transformation & Re-framing:** Handles dynamic cropping and aspect ratio scaling from 16:9 landscape to 9:16 vertical using **MoviePy** and direct **FFmpeg** filters.
- **Speech-to-Text & Subtitle Synchronization:** Powered by **Whisper**, extracting timestamps down to word-level segments to drive dynamic, word-by-word highlighted captions.
- **Audio/Video Composition:** Merges background audio, normalized voice tracks, and overlay assets into an automated composition graph.
- **Batch Export Engine:** Configured with hardware-accelerated video encoders (H.264/NVENC where supported) to minimize rendering bottlenecks during continuous generation.

---

## 🚀 Getting Started

### Prerequisites

- Python 3.9+
- **FFmpeg** installed and accessible in your system `PATH`:
  ```bash
  # Windows (via winget)
  winget install Gyan.FFmpeg

  # macOS (via Homebrew)
  brew install ffmpeg

  # Linux (Ubuntu/Debian)
  sudo apt install ffmpeg
  ```

### Installation

1. Clone the repository:
   ```bash
   git clone [https://github.com/kairos8713/utube-shorts.git](https://github.com/kairos8713/utube-shorts.git)
   cd utube-shorts
   ```

2. Set up a virtual environment and install dependencies:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Run the pipeline:
   ```bash
   python main.py
   ```

---

## ⚠️ Known Constraints & Technical Debt

- **CPU-Bound Rendering:** Without dedicated GPU acceleration flags configured in local environments, complex text overlay rendering defaults to CPU threads via MoviePy/Pillow, increasing render times on multi-layered clips.
- **Dynamic Text Reflow:** Aggressive font scaling can occasionally trigger boundary wrapping inconsistencies on multi-line text segments. A strict word-bounding box system is slated for the next iteration.

---

## 🔮 Roadmap

| Area | Planned Features |
|---|---|
| **Automated Publishing** | Direct OAuth2 integration with YouTube Data API v3 for hands-off scheduled uploads, metadata tag injection, and thumbnail uploads. |
| **Speaker Tracking** | Computer vision-based face detection (OpenCV/MediaPipe) to dynamically crop and center speakers in multi-person landscape video sources. |
| **Hardware Encoding** | Native NVENC/VAAPI direct hardware export presets to cut multi-clip batch rendering times. |
| **Audio Enhancement** | Automated background noise removal, spectral ducking, and dynamic voice leveling before speech-to-text inference. |

---

*Developed as a solo project by Emre Ercan.*
