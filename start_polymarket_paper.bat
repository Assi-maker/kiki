@echo off
rem Polymarket liquidity-rewards PAPER test (2026-10-02, pre-registered).
rem Run by the Task Scheduler task "polymarket_paper" (restarts on crash, single instance by OS lock).
rem Evaluate: .venv\Scripts\python.exe -m crypto_trading.polymarket.evaluate
cd /d C:\Users\asmat\ClaudeProjects
".venv\Scripts\pythonw.exe" -m crypto_trading.polymarket.paper
