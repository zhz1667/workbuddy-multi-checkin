@echo off
rem =====================================================================
rem  PURE ASCII ONLY (cmd parses .bat with the OEM codepage).
rem  Launches Edge with the existing session profile plus net-log, waits for
rem  the page to call the API, then extracts the Bearer token from the log.
rem  No DevTools clicks required.
rem =====================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

set "PY="
for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
    if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
)
if not defined PY if exist "D:\Python 3.12\python.exe" set "PY=D:\Python 3.12\python.exe"
if not defined PY (
    for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY set "PY=%%P"
)
if not defined PY (
    echo [ERROR] Python not found.
    pause
    exit /b 2
)

set "PROFILE=%~dp0.browser-profile-1"
set "NETLOG=%~dp0netlog.json"
set "EXE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not exist "%EXE%" set "EXE=C:\Program Files\Microsoft\Edge\Application\msedge.exe"
if not exist "%EXE%" set "EXE=C:\Program Files\Google\Chrome\Application\chrome.exe"

if not exist "%PROFILE%" (
    echo [ERROR] Session profile not found: %PROFILE%
    echo Run the login bat first.
    pause
    exit /b 2
)
if not exist "%EXE%" (
    echo [ERROR] Edge/Chrome not found.
    pause
    exit /b 2
)

del /q "%NETLOG%" 2>nul
echo ================================================================
echo   Capture token from browser session (no DevTools needed)
echo ================================================================
echo.
echo   A browser window will open and load workbuddy.cn automatically.
echo   Do NOT close it. Waiting 30 seconds for the page to call the API...
echo.
echo ================================================================
echo.

start "" "%EXE%" --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check --log-net-log="%NETLOG%" --net-log-capture-mode=IncludeSensitive "https://www.workbuddy.cn/"

ping -n 31 127.0.0.1 >nul

echo.
"%PY%" wb_checkin_multi.py --from-netlog "%NETLOG%"
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [OK] Token captured and saved.
) else (
    echo [FAILED] exit code = %RC%
)
echo.
echo You may close the browser window now.
pause
endlocal
