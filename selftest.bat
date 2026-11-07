@echo off
chcp 65001 >nul
rem ===========================================================================
rem  frame_parser self-test  (ASCII-only on purpose, see run.bat)
rem  Runs head-less (Qt offscreen platform), no display / serial port needed.
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title frame_parser selftest

set "PYTHONIOENCODING=utf-8"
set "QT_QPA_PLATFORM=offscreen"
set "PY="

if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0venv\Scripts\python.exe" set "PY=%~dp0venv\Scripts\python.exe"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"

if not defined PY (
    echo [ERROR] Python not found.
    pause
    exit /b 1
)

%PY% -c "import PyQt6" >nul 2>nul
if errorlevel 1 (
    echo [INFO] PyQt6 not found, installing ...
    %PY% -m pip install -r "%~dp0requirements.txt"
)

%PY% "%~dp0selftest.py"
set "CODE=%ERRORLEVEL%"
echo.
if "%CODE%"=="0" (
    echo [SELFTEST PASSED]
) else (
    echo [SELFTEST FAILED] see the list above
)
pause
endlocal
