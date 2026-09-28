@echo off
title crypto_trading bot
cd /d C:\Users\asmat\ClaudeProjects
if not exist logs mkdir logs
rem The bot itself writes its rotated log to logs\crypto_trading.log (and hard
rem crash dumps to logs\faulthandler.log). This file only records WHEN the
rem process started and exited, and with which exit code - the one fact the
rem Python process can never write about itself after it has died.
echo %date% %time% START >> logs\bot_process.log
.venv\Scripts\python.exe -m crypto_trading.run
echo %date% %time% EXIT code=%errorlevel% >> logs\bot_process.log
echo.
echo Boten har stoppats (exit code %errorlevel%). Se logs\crypto_trading.log
pause
