@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo =============================================
echo   Claude JustDoWork Recovery Proxy - v1.0
echo =============================================
echo.

rem === Virtual Environment ===
if not exist ".venv\Scripts\python.exe" (
    echo [Setup] Creating virtual environment...
    python -m venv .venv >nul 2>&1
)

if not exist ".venv\Scripts\python.exe" (
    echo [Error] Could not create a virtual environment. Is Python installed and on PATH?
    pause
    exit /b 1
)

rem === Install dependencies ===
echo [Setup] Installing packages (flask, requests, pystray, pillow)...
".venv\Scripts\python.exe" -m pip install -q flask requests pystray pillow
if errorlevel 1 (
    echo [Error] Dependency install failed. Run it again without -q to see why.
    pause
    exit /b 1
)

rem === API Key ===
if defined UPSTREAM_API_KEY goto :key_ready

if exist key.txt (
    set /p UPSTREAM_API_KEY=<key.txt
    goto :key_ready
)

echo [Setup] Enter your UPSTREAM_API_KEY (input is hidden, saved to key.txt):
for /f "usebackq delims=" %%K in (`powershell -NoProfile -Command "$s = Read-Host -AsSecureString; [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))"`) do set "UPSTREAM_API_KEY=%%K"

if not defined UPSTREAM_API_KEY goto :no_key

rem Write with no trailing space or newline so the key needs no cleanup on read.
<nul set /p "=!UPSTREAM_API_KEY!" >key.txt
echo [OK] Key saved to key.txt (ignored by git - do not commit it)

:key_ready
if "%UPSTREAM_API_KEY%" == "" goto :no_key

rem === Launch detached ===
rem wscript + pythonw.exe means no console is attached to the tray or the proxy,
rem so both keep running after this window is closed.
echo [OK] Starting tray icon + proxy in the background...
wscript.exe "%~dp0start_tray.vbs"

echo.
echo [Info] Proxy: http://127.0.0.1:8181  (right-click the tray icon to manage)
echo [Info] You can close this window now - the proxy keeps running.
echo [Info] Logs: debug_log.txt (requests), proxy_out.log (crashes), tray.log
echo [Info] Request dumps are off by default. Set DEBUG_DUMP=1 to capture them.
echo =============================================
timeout /t 5 >nul
exit /b 0

:no_key
echo [Error] No API key provided. Exiting...
pause
exit /b 1
