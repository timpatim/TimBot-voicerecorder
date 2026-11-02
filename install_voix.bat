@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Installation de la voix - Discord Voice Recorder

rem Usage : install_voix.bat [auto^|cpu^|cu126^|cu128] [nom_environnement]
rem   Cree un environnement conda SEPARE pour Chatterbox ^(synthese vocale^), sans toucher a celui du bot.
set "TORCH_DEVICE=%~1"
if "%TORCH_DEVICE%"=="" set "TORCH_DEVICE=auto"
set "VOIX_ENV=%~2"
if "%VOIX_ENV%"=="" set "VOIX_ENV=voicerecorder-voix"

echo.
echo ============================================================
echo   Voix du bot ^(Chatterbox^) - installation dans "%VOIX_ENV%"
echo ============================================================
echo.

echo [1/4] Recherche de conda...
call "%~dp0_conda.bat"
if not defined CONDA_BAT (
    echo [ERREUR] conda est introuvable ^(voir README, section Installation^).
    goto :fail
)

echo [2/4] Environnement conda "%VOIX_ENV%" ^(Python 3.11^)...
call "%CONDA_BAT%" env list | findstr /R /B /C:"%VOIX_ENV% " >nul 2>&1
if errorlevel 1 (
    call "%CONDA_BAT%" create -n "%VOIX_ENV%" -c conda-forge --override-channels python=3.11 -y
    if errorlevel 1 goto :fail
) else (
    echo        Deja present, il est reutilise.
)
call "%CONDA_BAT%" activate "%VOIX_ENV%"
if errorlevel 1 goto :fail
python -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo [3/4] Installation de chatterbox-tts... cela peut prendre plusieurs minutes.
python -m pip install chatterbox-tts
if errorlevel 1 goto :fail
rem perth ^(filigrane de Chatterbox^) a besoin de pkg_resources, absent des setuptools recents
python -m pip install "setuptools<81"
if errorlevel 1 goto :fail

echo [4/4] PyTorch GPU ^(%TORCH_DEVICE%^)...
python tools\fix_voix_torch.py --device %TORCH_DEVICE%
if errorlevel 1 goto :fail

for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)"') do > ".voix_python" echo %%P
echo.
echo ============================================================
echo   Voix installee. Le bot utilisera automatiquement cet environnement.
echo   Il reste a activer la voix : activer = oui dans la section [voix] de config.ini.
echo ============================================================
goto :end

:fail
echo.
echo [ECHEC] L'installation de la voix s'est arretee sur une erreur ^(voir ci-dessus^).
pause
exit /b 1

:end
pause
exit /b 0
