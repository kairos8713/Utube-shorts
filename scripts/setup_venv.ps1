# setup_venv.ps1 - create the project's own Python environment (.venv) and the YouTube PO-token provider.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup_venv.ps1
#   options: -BasePython <python.exe>  -PotProviderVersion 2.0.2  -SkipPotProvider
#
# What it does
#   1. .venv in the repo root (separate from the global Python used by other pipelines)
#   2. torch 2.3.1 + torchvision 0.18.1 CUDA 11.8 builds, then requirements.txt
#   3. newest yt-dlp + yt-dlp-ejs + bgutil-ytdlp-pot-provider (yt-dlp PO-Token-Guide, edited 2026-07:
#      "Use a PO Token Provider plugin to provide the mweb client with a PO Token for GVS requests")
#   4. bgutil provider server (HTTP mode, default 127.0.0.1:4416) built from source with Node >= 22
#      into tools\bgutil-ytdlp-pot-provider\server  (git clone --branch <version> ; npm ci ; npx tsc)
#      install_autostart.ps1 registers it as the "Reels-PotProvider" task.
#      Docker alternative (not used here):
#        docker run --name bgutil-provider -d --init -p 127.0.0.1:4416:4416 brainicism/bgutil-ytdlp-pot-provider
#   5. smoke checks (python, CUDA, yt-dlp version)
#
# NOTE: written unattended and NOT executed. Check the latest bgutil release tag first:
#   https://github.com/Brainicism/bgutil-ytdlp-pot-provider/releases

param(
    [string]$BasePython = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    [string]$PotProviderVersion = "2.0.2",
    [switch]$SkipPotProvider
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Run($exe, [string[]]$argv) {
    & $exe @argv
    if ($LASTEXITCODE -ne 0) { throw "failed ($LASTEXITCODE): $exe $($argv -join ' ')" }
}

if (-not (Test-Path $BasePython)) { throw "Base Python not found: $BasePython (pass -BasePython)" }

Step "1/5 virtual environment: $Venv"
if (-not (Test-Path $Py)) { Run $BasePython @("-m", "venv", $Venv) }
Run $Py @("-m", "pip", "install", "-U", "pip", "wheel")

Step "2/5 torch + torchvision (CUDA 11.8) and requirements.txt"
Run $Py @("-m", "pip", "install", "torch==2.3.1", "torchvision==0.18.1",
          "--index-url", "https://download.pytorch.org/whl/cu118")
Run $Py @("-m", "pip", "install", "-r", (Join-Path $Root "requirements.txt"))

Step "3/5 newest yt-dlp + EJS + PO token provider plugin"
Run $Py @("-m", "pip", "install", "-U", "yt-dlp[default]", "yt-dlp-ejs", "bgutil-ytdlp-pot-provider")

if (-not $SkipPotProvider) {
    Step "4/5 bgutil PO token server $PotProviderVersion (Node >= 22)"
    $node = (Get-Command node -ErrorAction SilentlyContinue).Source
    if (-not $node) { $node = "C:\Program Files\nodejs\node.exe" }
    if (-not (Test-Path $node)) { throw "Node.js not found - install Node 22+ or rerun with -SkipPotProvider" }
    $nodeMajor = [int](((& $node --version) -replace '^v', '').Split('.')[0])
    if ($nodeMajor -lt 22) { throw "Node $nodeMajor found, bgutil needs >= 22" }
    $git = (Get-Command git -ErrorAction SilentlyContinue).Source
    if (-not $git) { throw "git not found (needed to clone the provider)" }
    $npm = Join-Path (Split-Path $node) "npm.cmd"
    $npx = Join-Path (Split-Path $node) "npx.cmd"

    $tools = Join-Path $Root "tools"
    $repo = Join-Path $tools "bgutil-ytdlp-pot-provider"
    New-Item -ItemType Directory -Force $tools | Out-Null
    if (Test-Path $repo) {
        Write-Host "existing checkout found: $repo (delete it to re-clone another version)"
    } else {
        Run $git @("clone", "--single-branch", "--branch", $PotProviderVersion,
                   "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git", $repo)
    }
    Push-Location (Join-Path $repo "server")
    try {
        Run $npm @("ci")
        Run $npx @("tsc")
    } finally { Pop-Location }
    Write-Host "provider built: node $repo\server\build\main.js  (listens on 127.0.0.1:4416)"
} else {
    Step "4/5 PO token server skipped (-SkipPotProvider)"
}

Step "5/5 smoke checks"
Run $Py @("-c", "import sys, torch, cv2, whisper, flask; print(sys.version.split()[0], 'cuda', torch.cuda.is_available(), torch.cuda.device_count())")
Run $Py @("-m", "yt_dlp", "--version")
Write-Host ""
Write-Host "Done. Next:"
Write-Host "  - start the provider once:  node tools\bgutil-ytdlp-pot-provider\server\build\main.js"
Write-Host "  - verify: .venv\Scripts\python.exe -m yt_dlp -v --skip-download <youtube url>"
Write-Host "      expect '[pot] PO Token Providers: bgutil:http-...' and '[pot:bgutil:http] Generating a gvs PO Token ...'"
Write-Host "  - then register the services: powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1"
