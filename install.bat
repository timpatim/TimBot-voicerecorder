@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Installation - Discord Voice Recorder

rem Usage : install.bat [auto^|cpu^|cu126^|cu128] [nom_environnement]
rem   1er argument : build PyTorch ^(auto = detection du GPU NVIDIA^)
rem   2e argument  : nom de l'environnement conda ^(defaut : voicerecorder^)
set "TORCH_DEVICE=%~1"
if "%TORCH_DEVICE%"=="" set "TORCH_DEVICE=auto"
set "ENV_NAME=%~2"

echo.
echo ============================================================
echo   Discord Voice Recorder - installation ^(conda^)
echo ============================================================
echo.

rem ---------------------------------------------------------- 1. conda
echo [1/5] Recherche de conda...
call "%~dp0_conda.bat"
if not defined CONDA_BAT (
    echo.
    echo [ERREUR] conda est introuvable. Il doit etre installe au prealable :
    echo          Miniconda : https://www.anaconda.com/docs/getting-started/miniconda/install
    echo          Miniforge : https://github.com/conda-forge/miniforge
    echo          Installez-le, puis ouvrez une NOUVELLE fenetre ^(ou "Anaconda Prompt"^) et relancez install.bat.
    goto :fail
)
echo        conda trouve : %CONDA_BAT%
echo        Environnement : %ENV_NAME%

rem ---------------------------------------------------------- 2. Environnement ^(Python 3.12 + ffmpeg + git^)
echo [2/5] Environnement conda "%ENV_NAME%"...
call "%CONDA_BAT%" env list | findstr /R /B /C:"%ENV_NAME% " >nul 2>&1
if errorlevel 1 (
    echo        Creation avec Python 3.12, ffmpeg et git ^(canal conda-forge^)...
    call "%CONDA_BAT%" create -n "%ENV_NAME%" -c conda-forge --override-channels python=3.12 ffmpeg git -y
    if errorlevel 1 goto :fail
) else (
    echo        Deja present, il est reutilise ^(ffmpeg et git verifies^).
    call "%CONDA_BAT%" install -n "%ENV_NAME%" -c conda-forge --override-channels ffmpeg git -y
    if errorlevel 1 goto :fail
)
> ".conda_env" echo %ENV_NAME%

call "%CONDA_BAT%" activate "%ENV_NAME%"
if errorlevel 1 goto :fail
python -m pip install --upgrade pip
if errorlevel 1 goto :fail

rem ---------------------------------------------------------- 3. PyTorch ^(CPU ou CUDA selon le GPU^)
echo [3/5] Installation de PyTorch ^(%TORCH_DEVICE%^)... cela peut prendre plusieurs minutes.
python tools\install_torch.py --device %TORCH_DEVICE%
if errorlevel 1 goto :fail

rem ---------------------------------------------------------- 4. Dependances
echo [4/5] Installation des dependances Python...
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail

rem ---------------------------------------------------------- 5. Verification
echo [5/5] Verification de l'installation
echo ------------------------------------------------------------
python tools\check_env.py

echo.
echo ============================================================
echo   Installation terminee.
echo ============================================================
echo   Etapes restantes, a faire a la main ^(voir README.md^) :
echo     1. Creer le bot Discord et recuperer son jeton.
echo     2. Definir le jeton : setx DISCORD_TOKEN "votre_jeton"   puis ouvrir un nouveau terminal.
echo     3. Optionnel, pour Google Drive : placer credentials.json dans ce dossier.
echo     4. Lancer le bot : run.bat
echo.
goto :end

:fail
echo.
echo [ECHEC] L'installation s'est arretee sur une erreur. Corrigez le probleme indique ci-dessus et relancez install.bat.
echo.
pause
exit /b 1

:end
pause
exit /b 0
