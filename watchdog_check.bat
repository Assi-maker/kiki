@echo off
rem External bot watchdog (2026-09-28). Run by Windows Task Scheduler every
rem 5 minutes: alerts on Telegram when logs\heartbeat.json is missing or older
rem than 3 minutes (process dead, frozen or the PC asleep). Alert-only - it
rem never starts, stops or trades anything.
cd /d C:\Users\asmat\ClaudeProjects
.venv\Scripts\python.exe -m crypto_trading.watchdog
