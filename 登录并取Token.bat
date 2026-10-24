@echo off
rem =====================================================================
rem  PURE ASCII ONLY. Do NOT put Chinese text in this file.
rem  cmd.exe parses .bat with the OEM codepage; UTF-8 Chinese breaks it
rem  and produces "not recognized as an internal command" errors.
rem  All Chinese messages are printed by wb_checkin_multi.py instead.
rem =====================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem ---- slot: 1 = first account session, 2 = second account session ----
set "SLOT=%~1"
if "%SLOT%"=="" set "SLOT=1"

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
    echo [ERROR] Python not found. Install Python 3.8+ or edit this .bat to set PY.
    echo.
    pause
    exit /b 2
)

chcp 65001 >nul
"%PY%" wb_checkin_multi.py --browser-login --wait-login 15 --browser-profile ".browser-profile-%SLOT%"
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [OK] Token saved to accounts.json.
) else (
    echo [FAILED] exit code = %RC%
)
echo.
pause
endlocal
