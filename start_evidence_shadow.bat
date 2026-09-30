@echo off
title evidence shadow service (read-only, no trading)
cd /d C:\Users\asmat\ClaudeProjects
rem Historical Evidence Layer stage 2 shadow service (2026-09-30). Reads the
rem bot DB read-only and writes only data\evidence_shadow.db - it never trades
rem and the bot never imports it. Restarts itself 60 s after an exit. The pause
rem uses ping, not timeout: timeout fails at once without an interactive
rem console, which turned the restart loop into a busy spin.
:loop
echo %date% %time% START >> data\evidence_shadow_service.log
.venv\Scripts\python.exe -u -m crypto_trading.evidence_shadow.service >> data\evidence_shadow_service.log 2>&1
echo %date% %time% EXIT code=%errorlevel% - restart in 60 s >> data\evidence_shadow_service.log
ping -n 61 127.0.0.1 > nul
goto loop
