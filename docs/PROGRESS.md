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
- Whisper `small.pt` cached. Practical-RIFE + flownet.pkl present.
- Installed tonight: pytest, cryptography, imagehash, boto3, dulwich

## Phase status
| Phase | Status | Notes |
|---|---|---|
| 0 Setup (backup, git, docs) | DONE | backups/pre_migration_20261019, baseline commit 6c7b7c09f5 |
| 1 Security | IN PROGRESS | |
| 2 Cleanup + IG format | TODO | |
| 3 Qwen/Ollama + GPU lock | TODO | |
| 4 3-day cycle, schema, guardrails, brain | TODO | |
| 5 IG publisher + scheduler | TODO | |
| 6 Comment responder | TODO | |
| 7 Source discovery / fast lane | TODO | |
| 8 Analytics feedback | TODO | |
| 9 Web panel | TODO | |
| 10 Ops / resilience | TODO | |
| 11 Tests + E2E dry run | TODO | |
| 12 Morning report | TODO | |

## Next step
Phase 1: config.py + .env.example, crypto_utils.py, scripts/init_secrets.py, panel auth.
