@echo off
title Polymarket Bot — Dry Run
cd /d "%~dp0"
echo Iniciando Polymarket Bot em modo dry_run...
echo Logs em: data\logs\
echo Pressione Ctrl+C para encerrar.
echo.
.venv\Scripts\python.exe main.py
pause
