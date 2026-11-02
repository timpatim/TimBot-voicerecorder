@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Installation de la voix OmniVoice - Discord Voice Recorder

rem Usage : install_omnivoice.bat [auto^|cpu^|cu126^|cu128] [nom_environnement]
rem   Cree un environnement conda SEPARE pour OmniVoice ^(synthese vocale^), sans toucher a celui du bot.
rem   OmniVoice demande torch 2.8 : on reutilise tools\install_torch.py ^(meme version que WhisperX^).
set "TORCH_DEVICE=%~1"
if "%TORCH_DEVICE%"=="" set "TORCH_DEVICE=auto"
set "VOIX_ENV=%~2"
if "%VOIX_ENV%"=="" set "VOIX_ENV=voicerecorder-omnivoice"

echo.
echo ============================================================
echo   Voix du bot ^(OmniVoice^) - installation dans "%VOIX_ENV%"
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
    call "%CONDA_BAT%" create -n "%VOIX_ENV%" -c conda-forge --override-channels python=3.11 ffmpeg -y
    if errorlevel 1 goto :fail
) else (
    echo        Deja present, il est reutilise.
)
call "%CONDA_BAT%" activate "%VOIX_ENV%"
if errorlevel 1 goto :fail
python -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo [3/4] PyTorch ^(%TORCH_DEVICE%^)...
if /I "%TORCH_DEVICE%"=="auto" (
    python tools\install_torch.py --no-prompt
) else (
    python tools\install_torch.py --no-prompt --device %TORCH_DEVICE%
)
if errorlevel 1 goto :fail

echo [4/4] Installation d'omnivoice... cela peut prendre plusieurs minutes.
python -m pip install omnivoice soundfile
if errorlevel 1 goto :fail

for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)"') do > ".voix_python_omnivoice" echo %%P
echo.
echo ============================================================
echo   OmniVoice installe. Le bot utilisera automatiquement cet environnement.
echo   Il reste a activer la voix : activer = oui dans la section [voix] de config.ini
echo   ^(moteur = omnivoice, valeur par defaut^).
echo   Le premier lancement telecharge le modele ^(plusieurs Go^).
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
