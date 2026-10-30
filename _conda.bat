@echo off
rem Sous-programme commun a install.bat et run.bat : localise conda.
rem Resultat : CONDA_BAT (chemin de condabin\conda.bat, vide si introuvable) et ENV_NAME.
set "CONDA_BAT="
if defined CONDA_EXE if exist "%CONDA_EXE:\Scripts\conda.exe=\condabin\conda.bat%" set "CONDA_BAT=%CONDA_EXE:\Scripts\conda.exe=\condabin\conda.bat%"
if not defined CONDA_BAT (
    for /f "delims=" %%C in ('where conda.bat 2^>nul') do if not defined CONDA_BAT set "CONDA_BAT=%%C"
)
if not defined CONDA_BAT (
    for %%D in ("%USERPROFILE%\miniconda3" "%USERPROFILE%\anaconda3" "%USERPROFILE%\miniforge3" "%USERPROFILE%\mambaforge" "%LOCALAPPDATA%\miniconda3" "%LOCALAPPDATA%\anaconda3" "%ProgramData%\miniconda3" "%ProgramData%\anaconda3" "C:\miniconda3" "C:\anaconda3") do (
        if not defined CONDA_BAT if exist "%%~D\condabin\conda.bat" set "CONDA_BAT=%%~D\condabin\conda.bat"
    )
)
rem Nom de l'environnement : memorise par install.bat dans .conda_env, sinon valeur par defaut.
if not defined ENV_NAME (
    if exist ".conda_env" (set /p ENV_NAME=<".conda_env") else set "ENV_NAME=voicerecorder"
)
exit /b 0
