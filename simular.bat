@echo off
title Polymarket Bot — Simulação
cd /d "%~dp0"
echo Rodando simulação contra o Polymarket real...
echo Resultados em: data\trades\signals.csv
echo.
.venv\Scripts\python.exe simulate.py
echo.
pause
