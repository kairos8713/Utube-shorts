# stop_services.ps1 - stop every Reels service, including child processes.
#
#   powershell -ExecutionPolicy Bypass -File scripts\stop_services.ps1            # stop
#   powershell -ExecutionPolicy Bypass -File scripts\stop_services.ps1 -DryRun    # only list
#
# 1. Task Scheduler: running Reels-* tasks are STOPPED (Stop-ScheduledTask). They are NOT removed and
#    start again at the next logon (remove them with install_autostart.ps1 -Uninstall).
# 2. Processes whose command line runs one of this repo's services (panel, watchdog, publish_runner,
#    comment_responder, cycle_runner, the full-test service wrapper) or the bgutil PO token server are
#    killed together with their whole child tree: the .venv python.exe is a launcher that
#    starts the real interpreter as a child, so killing only the parent leaves the service running.
# Ollama is not touched. Watchdog first, so it cannot restart what is being stopped.

param([switch]$DryRun)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Scripts = @("watchdog.py", "panel.py", "publish_runner.py", "comment_responder.py", "cycle_runner.py",
             "run_service.py")
$TaskNames = @("Reels-Watchdog", "Reels-Panel", "Reels-Publisher", "Reels-Comments", "Reels-CycleRunner",
               "Reels-PotProvider")

# 1. scheduled tasks
foreach ($n in $TaskNames) {
    $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
    if ($t -and $t.State -eq "Running") {
        if ($DryRun) { Write-Host "[dry-run] would stop task $n" }
        else { Stop-ScheduledTask -TaskName $n; Write-Host "stopped task $n (kept, not removed)" }
    }
}

# 2. processes (+ child trees)
function Get-ReelsProcesses {
    $rootPattern = [regex]::Escape($Root)
    Get-CimInstance Win32_Process | Where-Object {
        $cmd = $_.CommandLine
        $cmd -and $_.ProcessId -ne $PID -and (
            ($cmd -match $rootPattern -and ($Scripts | Where-Object { $cmd -match [regex]::Escape($_) })) -or
            $cmd -match "bgutil-ytdlp-pot-provider\\server\\build\\main\.js")
    } | ForEach-Object {
        [pscustomobject]@{
            Pid  = $_.ProcessId
            Name = $_.Name
            What = (([regex]::Matches($_.CommandLine, "(watchdog|panel|publish_runner|comment_responder|cycle_runner|run_service)\.py|main\.js") |
                     ForEach-Object { $_.Value }) -join " ")
        }
    }
}

function Stop-Tree([int]$id) {
    # children first (by ParentProcessId), then the process itself; already-exited ones are skipped
    Get-CimInstance Win32_Process -Filter "ParentProcessId = $id" | ForEach-Object { Stop-Tree $_.ProcessId }
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
}

$procs = @(Get-ReelsProcesses)
# watchdog first, so it cannot restart the others while they are being stopped
$procs = @($procs | Sort-Object @{ Expression = { if ($_.What -match "watchdog") { 0 } else { 1 } } })
if (-not $procs) { Write-Host "no Reels service processes running" }
foreach ($p in $procs) {
    if ($DryRun) { Write-Host "[dry-run] would kill pid $($p.Pid) ($($p.Name): $($p.What)) and its children"; continue }
    if (-not (Get-Process -Id $p.Pid -ErrorAction SilentlyContinue)) { continue }   # went down with its child
    Stop-Tree $p.Pid
    Write-Host "killed pid $($p.Pid) ($($p.What)) + children"
}

if (-not $DryRun) {
    Start-Sleep -Seconds 2
    $left = @(Get-ReelsProcesses)
    if ($left) {
        Write-Host "STILL RUNNING:" ($left | ForEach-Object { "$($_.Pid) $($_.What)" })
        exit 1
    }
    Write-Host "all Reels services stopped"
}
