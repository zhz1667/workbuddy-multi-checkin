@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem ---- locate a Python interpreter (managed runtime first, then PATH) ----
set "PY="
for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
    if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
)
if not defined PY if exist "D:\Python 3.12\python.exe" set "PY=D:\Python 3.12\python.exe"
if not defined PY (
    for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY set "PY=%%P"
)

if not defined PY (
    echo.
    echo [ERROR] Python not found. Install Python 3.8+ or edit this .bat to set PY manually.
    echo.
    pause
    exit /b 2
)

"%PY%" wb_menu.py
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [INFO] exit code = %RC%
)
echo.
pause
endlocal
