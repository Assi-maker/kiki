@echo off
rem P1 (2026-10-01): the bot runs under its supervisor (no console window,
rem one instance, restart after a crash, orderly stop). This file no longer
rem starts crypto_trading.run directly - that would bypass the supervisor.
rem Preferred: the Task Scheduler task "crypto_trading_bot"
rem (install_bot_service.ps1). This is the manual equivalent.
cd /d C:\Users\asmat\ClaudeProjects
start "" ".venv\Scripts\pythonw.exe" -m crypto_trading.supervisor run
echo Supervisor started in the background.
echo Status: .venv\Scripts\python.exe -m crypto_trading.supervisor status
echo Stop:   stop_bot.bat
