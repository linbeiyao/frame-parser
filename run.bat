@echo off
chcp 65001 >nul
rem ===========================================================================
rem  frame_parser launcher  (this file is intentionally ASCII-only: cmd.exe
rem  parses .bat files with the OEM/ANSI code page, so non-ASCII comments can
rem  break the script on some systems - keep it ASCII.)
rem
rem  Usage:
rem    run.bat                 start the GUI (loads a demo preset)
rem    run.bat  capture.bin    start and import that file
rem
rem  Picks python from: .venv / venv / env in this folder, then py -3, then
rem  python on PATH. Installs PyQt6 from requirements.txt when missing.
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title frame_parser - data frame parser

set "PYTHONIOENCODING=utf-8"
set "PY="

if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0venv\Scripts\python.exe" set "PY=%~dp0venv\Scripts\python.exe"
if not defined PY if exist "%~dp0env\Scripts\python.exe" set "PY=%~dp0env\Scripts\python.exe"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"

if not defined PY (
    echo [ERROR] Python not found. Install Python 3.9+ and tick "Add Python to PATH".
    echo         https://www.python.org/downloads/windows/
    pause
    exit /b 1
)

%PY% --version
if errorlevel 1 (
    echo [ERROR] cannot run: %PY%
    pause
    exit /b 1
)

%PY% -c "import PyQt6" >nul 2>nul
if errorlevel 1 (
    echo [INFO] PyQt6 not found, installing dependencies ...
    %PY% -m pip install --upgrade pip
    %PY% -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo [ERROR] install failed. Run manually:  %PY% -m pip install PyQt6
        pause
        exit /b 1
    )
)

echo [RUN] %PY% main.py %*
%PY% "%~dp0main.py" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo [ERROR] exited with code %CODE%
    pause
    exit /b %CODE%
)
endlocal
