@echo off
setlocal
title BetterCallTrader v13.0
cd /d "%~dp0"
echo Starting BetterCallTrader v13.0...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" py\research_bot.py
) else (
    python py\research_bot.py
)
pause
