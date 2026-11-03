# Register a daily Windows Scheduled Task for the watch.
#
#   powershell -ExecutionPolicy Bypass -File scripts/schedule_watch.ps1
#
# Runs at 07:15 local time. Output lands in reports/out/<date>/digest.md.
# Remove with: schtasks /Delete /TN SENTINEL-watch /F

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$log = Join-Path $root "data\watch_task.log"

$action = New-ScheduledTaskAction -Execute $python `
  -Argument "-m sentinel.cli watch --days 2" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At 07:15
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
  -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName "SENTINEL-watch" -Action $action -Trigger $trigger `
  -Settings $settings -Description "SENTINEL daily filing digest" -Force | Out-Null

Write-Host "registered SENTINEL-watch; logs to $log"
