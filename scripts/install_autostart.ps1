# install_autostart.ps1 - register the Reels services in Windows Task Scheduler (run at logon).
#
#   Run in a normal (non-admin is fine) PowerShell, from the repo folder:
#     powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
#   Remove again:
#     powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Uninstall
#
# Tasks (current user, at logon, staggered by 1 minute, restart on failure 3x / 5 min,
# no time limit, run on battery):
#   Reels-Panel, Reels-Watchdog, Reels-Publisher, Reels-Comments, Reels-CycleRunner
#
# NOTE: this script was written unattended and has NOT been executed.

param(
    [switch]$Uninstall,
    [string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python311\pythonw.exe"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

$Services = @(
    @{ Name = "Reels-Panel";       Script = "panel.py";             DelayMin = 1 },
    @{ Name = "Reels-Watchdog";    Script = "watchdog.py";          DelayMin = 2 },
    @{ Name = "Reels-Publisher";   Script = "publish_runner.py";    DelayMin = 3 },
    @{ Name = "Reels-Comments";    Script = "comment_responder.py"; DelayMin = 4 },
    @{ Name = "Reels-CycleRunner"; Script = "cycle_runner.py";      DelayMin = 5 }
)

if ($Uninstall) {
    foreach ($s in $Services) {
        if (Get-ScheduledTask -TaskName $s.Name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $s.Name -Confirm:$false
            Write-Host "removed $($s.Name)"
        }
    }
    return
}

if (-not (Test-Path $Python)) {
    throw "Python not found at $Python (pass -Python <path to pythonw.exe>)"
}
if (-not (Test-Path (Join-Path $Root ".env"))) {
    throw ".env missing in $Root - run scripts\init_secrets.py first"
}

$user = "$env:USERDOMAIN\$env:USERNAME"
foreach ($s in $Services) {
    $action = New-ScheduledTaskAction -Execute $Python -Argument "`"$(Join-Path $Root $s.Script)`"" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $trigger.Delay = "PT$($s.DelayMin)M"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $s.Name -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description "Instagram Reels automation: $($s.Script)" -Force | Out-Null
    Write-Host "registered $($s.Name) -> $($s.Script) (logon + $($s.DelayMin) min)"
}

Write-Host ""
Write-Host "Done. Also set Windows Update active hours (Settings > Windows Update > Advanced options >"
Write-Host "Active hours) to cover the posting window (15:00-05:00 Warsaw) so updates do not reboot mid-run,"
Write-Host "and disable sleep for the plugged-in power plan."
