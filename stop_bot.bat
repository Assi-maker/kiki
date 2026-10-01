@echo off
rem Orderly stop (P1, 2026-10-01): the bot finishes its current LIVE tick and
rem exits; the supervisor then exits. Nothing on the exchange is cancelled or
rem closed - exchange-side SL/TP stay; the next start reconciles.
cd /d C:\Users\asmat\ClaudeProjects
.venv\Scripts\python.exe -m crypto_trading.supervisor stop
