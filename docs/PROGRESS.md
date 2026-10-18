# PROGRESS — utube-shorts → Instagram Reels migration

> If the session restarts: read this file first, then `docs/DECISIONS.md`, then continue
> from "Next step". Python is NOT on PATH:
> `C:\Users\Emre\AppData\Local\Programs\Python\Python311\python.exe`.
> git is NOT installed; use `python scripts/dev_git.py commit "msg"` (dulwich, local only, no push).
> Branch: `instagram-migration`. Tests: `python -m pytest -q tests`.

## Environment (detected 2026-10-19)
- 2× RTX 3060 12 GB (driver 610.88), CUDA visible to torch (2 devices)
- Python 3.11.9, FFmpeg 2026-08-03 essentials (gyan.dev), Node at C:\Program Files\nodejs
- Ollama: NOT installed. `C:\models\Qwen3-32B-Q4_K_M.gguf`: NOT present.
- yt-dlp real downloads fail: HTTP 403 (GVS PO token) — E2E uses `--synthetic`.
- docs/MORNING_REPORT.md drafted; placeholders E2E_* / TEST_COUNT / COMMITS to fill at the end.
- Whisper `small.pt` cached. Practical-RIFE + flownet.pkl present.
- Installed tonight: pytest, cryptography, imagehash, boto3, dulwich

## Phase status
| Phase | Status | Notes |
|---|---|---|
| 0 Setup (backup, git, docs) | DONE | backups/pre_migration_20261019, baseline commit 6c7b7c09f5 |
| 1 Security | DONE | commit 1110d847df; .env created (keys generated), DB password encrypted |
| 2 Cleanup + IG format | DONE | commit ec0bb574a4; real RIFE run verified 60fps/8.9Mbps/-14.4 LUFS |
| 3 Qwen/Ollama + GPU lock | DONE (Ollama BLOKE) | client+Modelfile+mock; no Ollama/GGUF on PC |
| 4 3-day cycle, schema, guardrails, brain | DONE | real DB migrated (002, 004) |
| 5 IG publisher + scheduler | DONE | docs verified (D-020); default upload r2_url; publish_runner.py |
| 6 Comment responder | DONE | |
| 7 Source discovery / fast lane | DONE | |
| 8 Analytics feedback | DONE | |
| 9 Web panel | DONE | |
| 10 Ops / resilience | DONE | autostart script written, NOT run |
| 11 Tests + E2E dry run | DONE | 203 tests pass; E2E PASS 13/13 (logs/e2e/20261018T235254Z) |
| 12 Morning report | DONE | docs/MORNING_REPORT.md |

## Next step
All phases done. Remaining work is Emre's checklist in docs/MORNING_REPORT.md §3
(Ollama + GGUF, Meta app, R2, yt-dlp 403 fix, panel password, autostart).
