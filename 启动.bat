@echo off
setlocal
title Buddy 2 API
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [Setup] Creating project virtual environment...
  py -3 -m venv .venv || python -m venv .venv
  if errorlevel 1 goto venv_error
  echo [Setup] Installing dependencies ^(this may take a while^)...
  .venv\Scripts\python.exe -m pip install -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com -r requirements.txt
  if errorlevel 1 goto dependency_error
)
REM Default: do not auto-open the browser. On this machine Edge often runs elevated,
REM and a non-elevated process launching Edge triggers the "restart existing instance
REM with regular permissions?" prompt. Set BUDDY_OPEN_BROWSER=1 to auto-open.
set "BROWSER_FLAG=--no-browser"
if /i "%BUDDY_OPEN_BROWSER%"=="1" set "BROWSER_FLAG="
echo [Start] http://127.0.0.1:8787
echo [Open] Open this URL manually in your browser (set BUDDY_OPEN_BROWSER=1 to auto-open)
echo [Stop] Ctrl+C
echo.
.venv\Scripts\python.exe server.py --port 8787 %BROWSER_FLAG% %*
if errorlevel 1 goto failed
echo [Exit] Server stopped. This window stays open for 30 seconds.
%SystemRoot%\System32\timeout.exe /t 30 /nobreak >nul
exit /b 0

:venv_error
echo [Error] Could not create the project virtual environment
goto failed
:dependency_error
echo [Error] Dependency installation failed
:failed
echo [Error] Buddy2api exited with a failure.
pause
exit /b 1
