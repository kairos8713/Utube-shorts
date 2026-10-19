# install_autostart.ps1 - register the Reels services in Windows Task Scheduler (run at logon).
#
#   Prerequisite: scripts\setup_venv.ps1 (creates .venv and builds the PO token provider).
#   Run in a normal (non-admin is fine) PowerShell, from the repo folder:
#     powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
#   Remove again:
#     powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Uninstall
#
# Tasks (current user, at logon, staggered, restart on failure 3x / 5 min, no time limit, run on battery):
#   Reels-PotProvider (node, bgutil PO token server on 127.0.0.1:4416 - only if built)
#   Reels-Panel, Reels-Watchdog, Reels-Publisher, Reels-Comments, Reels-CycleRunner
# All Python services run with the project's own .venv\Scripts\pythonw.exe.
#
# NOTE: this script was written unattended and has NOT been executed.

param(
    [switch]$Uninstall,
    [string]$Python = "",
    [string]$Node = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $Root ".venv\Scripts\pythonw.exe" }
if (-not $Node) {
    $Node = (Get-Command node -ErrorAction SilentlyContinue).Source
    if (-not $Node) { $Node = "C:\Program Files\nodejs\node.exe" }
}
$PotServer = Join-Path $Root "tools\bgutil-ytdlp-pot-provider\server"
$PotMain = Join-Path $PotServer "build\main.js"

$Services = @(
    @{ Name = "Reels-Panel";       Script = "panel.py";             DelayMin = 1 },
    @{ Name = "Reels-Watchdog";    Script = "watchdog.py";          DelayMin = 2 },
    @{ Name = "Reels-Publisher";   Script = "publish_runner.py";    DelayMin = 3 },
    @{ Name = "Reels-Comments";    Script = "comment_responder.py"; DelayMin = 4 },
    @{ Name = "Reels-CycleRunner"; Script = "cycle_runner.py";      DelayMin = 5 }
)
$AllNames = @("Reels-PotProvider") + ($Services | ForEach-Object { $_.Name })

if ($Uninstall) {
    foreach ($n in $AllNames) {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            Write-Host "removed $n"
        }
    }
    return
}

if (-not (Test-Path $Python)) {
    throw "Python not found at $Python - run scripts\setup_venv.ps1 first (or pass -Python <pythonw.exe>)"
}
if (-not (Test-Path (Join-Path $Root ".env"))) {
    throw ".env missing in $Root - run scripts\init_secrets.py first"
}

$user = "$env:USERDOMAIN\$env:USERNAME"

function Register-ReelsTask($name, $exe, $argument, $workdir, $delayMin, $description) {
    $action = New-ScheduledTaskAction -Execute $exe -Argument $argument -WorkingDirectory $workdir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $trigger.Delay = "PT$($delayMin)M"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description $description -Force | Out-Null
    Write-Host "registered $name (logon + $delayMin min)"
}

if (Test-Path $PotMain) {
    if (-not (Test-Path $Node)) { throw "node not found at $Node (pass -Node <node.exe>)" }
    Register-ReelsTask "Reels-PotProvider" $Node "`"$PotMain`"" $PotServer 0 `
        "bgutil yt-dlp PO token provider (127.0.0.1:4416)"
} else {
    Write-Host "PO token provider not built ($PotMain missing) - skipped. YouTube downloads may fail with HTTP 403."
}

foreach ($s in $Services) {
    Register-ReelsTask $s.Name $Python "`"$(Join-Path $Root $s.Script)`"" $Root $s.DelayMin `
        "Instagram Reels automation: $($s.Script)"
}

Write-Host ""
Write-Host "Done. Also set Windows Update active hours (Settings > Windows Update > Advanced options >"
Write-Host "Active hours) to cover the posting window (15:00-05:00 Warsaw) so updates do not reboot mid-run,"
Write-Host "and disable sleep for the plugged-in power plan."
