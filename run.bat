@echo off
setlocal
cd /d "%~dp0"
title Discord Voice Recorder

call "%~dp0_conda.bat"
if not defined CONDA_BAT (
    echo conda introuvable : lancez d'abord install.bat ^(voir README.md^)
    pause
    exit /b 1
)
call "%CONDA_BAT%" activate "%ENV_NAME%"
if errorlevel 1 (
    echo Environnement conda "%ENV_NAME%" introuvable : lancez d'abord install.bat
    pause
    exit /b 1
)

set PYTHONUTF8=1
python bot_v4.py %*
set "RC=%errorlevel%"

if not "%RC%"=="0" (
    echo.
    echo Le bot s'est arrete avec le code %RC%.
    pause
)
exit /b %RC%
