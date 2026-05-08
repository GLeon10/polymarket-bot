@echo off
cd /d "%~dp0"
for /f %%i in ('powershell -command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i
echo === Log de hoje: %TODAY% ===
echo.
if exist "data\logs\%TODAY%.log" (
    type "data\logs\%TODAY%.log"
) else (
    echo Nenhum log encontrado para hoje. O bot ja rodou hoje?
)
pause
