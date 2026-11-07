@echo off
chcp 65001 >nul
rem ===========================================================================
rem  frame_parser CLI wrapper (ASCII-only on purpose, see run.bat).
rem
rem    cli.bat                                  show help
rem    cli.bat --list-presets
rem    cli.bat --infer --file data\backward_sample.txt
rem    cli.bat --preset backward_eb90 --file data\backward_sample.txt --limit 20
rem    cli.bat --auto-extract --file logs\sample.jsonl --extract-filter 0x1E7 ^
rem            --mode fixed --fixed-len 8 --out can_1e7.csv
rem
rem  Exit codes: 0 ok / 1 error / 3 parsed but some frames failed checksum
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
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

if "%~1"=="" (
    %PY% "%~dp0cli.py" --help
    pause
    exit /b 0
)

%PY% "%~dp0cli.py" %*
set "CODE=%ERRORLEVEL%"
if "%CODE%"=="1" (
    echo.
    echo [cli.bat] failed with code 1
    pause
)
endlocal & exit /b %CODE%
