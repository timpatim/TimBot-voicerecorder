@echo off
rem Sous-programme commun a install.bat et run.bat : localise conda.
rem Resultat : CONDA_BAT (chemin de condabin\conda.bat, vide si introuvable) et ENV_NAME.
set "CONDA_BAT="

rem 1. Variable CONDA_EXE ^(definie quand on est dans un Anaconda Prompt ou un env active^)
if not defined CONDA_EXE goto :try_where
for %%F in ("%CONDA_EXE%") do set "CONDA_SCRIPTS=%%~dpF"
if exist "%CONDA_SCRIPTS%..\condabin\conda.bat" set "CONDA_BAT=%CONDA_SCRIPTS%..\condabin\conda.bat"
if defined CONDA_BAT goto :found

rem 2. conda.bat present dans le PATH
:try_where
for /f "delims=" %%C in ('where conda.bat 2^>nul') do if not defined CONDA_BAT set "CONDA_BAT=%%C"
if defined CONDA_BAT goto :found

rem 3. Dossiers d'installation habituels
for %%D in ("%USERPROFILE%\miniconda3" "%USERPROFILE%\anaconda3" "%USERPROFILE%\miniforge3" "%USERPROFILE%\mambaforge" "%LOCALAPPDATA%\miniconda3" "%LOCALAPPDATA%\anaconda3" "%ProgramData%\miniconda3" "%ProgramData%\anaconda3" "C:\miniconda3" "C:\anaconda3") do if not defined CONDA_BAT if exist "%%~D\condabin\conda.bat" set "CONDA_BAT=%%~D\condabin\conda.bat"

:found
rem Nom de l'environnement : memorise par install.bat dans .conda_env, sinon valeur par defaut.
if defined ENV_NAME goto :done
set "ENV_NAME=voicerecorder"
if exist ".conda_env" set /p ENV_NAME=<".conda_env"
:done
exit /b 0
