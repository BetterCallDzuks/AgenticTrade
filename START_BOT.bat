@echo off
setlocal
title BetterCallTrader v12.0
cd /d "%~dp0"
echo Starting BetterCallTrader v12.0...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" research_bot.py
) else (
    python research_bot.py
)
pause
