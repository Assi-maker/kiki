# Registers the crypto_trading bot as a supervised background service
# (P1, 2026-10-01). The task starts the SUPERVISOR (no console window), which
# keeps exactly one bot running and restarts it after a crash:
#     .venv\Scripts\pythonw.exe -m crypto_trading.supervisor run
#
# Run as administrator  -> runs whether or not you are logged on (S4U) and
#                          starts at Windows startup: survives log-off and
#                          reboots without anyone logging in.
# Run as a normal user  -> starts at your log-on: survives closing terminals,
#                          ending Claude Code sessions, crashes and reboots
#                          (after you log on), but NOT logging off.
#
# The task is left DISABLED unless -Enable is given.
#   powershell -ExecutionPolicy Bypass -File install_bot_service.ps1           # install, disabled
#   powershell -ExecutionPolicy Bypass -File install_bot_service.ps1 -Enable   # install + enable
# Start now:  Start-ScheduledTask -TaskName crypto_trading_bot
# Stop:       stop_bot.bat   (orderly: the bot finishes its LIVE tick; SL/TP stay on the exchange)
param([switch]$Enable)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "missing $pythonw" }
$user = "$env:USERDOMAIN\$env:USERNAME"
$isAdmin = (New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "-m crypto_trading.supervisor run" -WorkingDirectory $root
if ($isAdmin) {
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $mode = "S4U, at startup (survives log-off)"
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $mode = "interactive, at log-on (does NOT survive log-off - rerun as admin for that)"
}
# IgnoreNew + the supervisor's own OS lock = never two supervisors; the bot's
# database lock = never two bots. Task Scheduler restarts a dead supervisor.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "crypto_trading_bot" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
if (-not $Enable) { Disable-ScheduledTask -TaskName "crypto_trading_bot" | Out-Null }
$state = (Get-ScheduledTask -TaskName "crypto_trading_bot").State
Write-Output "crypto_trading_bot registered: $mode; state=$state"
