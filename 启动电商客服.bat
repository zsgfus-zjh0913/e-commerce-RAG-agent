@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
title E-Commerce CS - Launcher
cd /d "%~dp0"

set "APP_URL=http://127.0.0.1:5000"
set "AGENT_URL=http://127.0.0.1:5000/agent/login"

echo.
echo  ============================================
echo      E-Commerce Customer Service System
echo      Starting...
echo  ============================================
echo.

if not exist "%~dp0app.py" (
    echo Project file app.py was not found.
    echo Make sure this launcher stays in the project folder.
    echo.
    pause
    exit /b 1
)

rem ===== 1. Find Python =====
set "PYEXE="
set "PYARGS="

if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE if exist "%~dp0venv\Scripts\python.exe" set "PYEXE=%~dp0venv\Scripts\python.exe"
if not defined PYEXE if exist "C:\Users\22013\AppData\Roaming\TRAE SOLO CN\ModularData\ai-agent\vm\tools\python\python.exe" set "PYEXE=C:\Users\22013\AppData\Roaming\TRAE SOLO CN\ModularData\ai-agent\vm\tools\python\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYEXE if exist "C:\Python313\python.exe" set "PYEXE=C:\Python313\python.exe"
if not defined PYEXE if exist "C:\Python312\python.exe" set "PYEXE=C:\Python312\python.exe"
if not defined PYEXE if exist "C:\Python311\python.exe" set "PYEXE=C:\Python311\python.exe"

if not defined PYEXE for /f "delims=" %%I in ('where python.exe 2^>nul') do if not defined PYEXE set "PYEXE=%%~fI"
if not defined PYEXE for /f "delims=" %%I in ('where py.exe 2^>nul') do if not defined PYEXE (set "PYEXE=%%~fI" & set "PYARGS=-3")

if not defined PYEXE goto :NO_PYTHON

echo [1/3] Python: "%PYEXE%" %PYARGS%
"%PYEXE%" %PYARGS% --version
if errorlevel 1 (
    echo.
    echo Python exists but cannot be started.
    echo.
    pause
    exit /b 1
)
echo.

rem ===== 2. Check dependencies =====
"%PYEXE%" %PYARGS% -c "import flask,sqlalchemy,requests,dotenv" >nul 2>&1
if errorlevel 1 (
    echo [2/3] First run, installing dependencies...
    "%PYEXE%" %PYARGS% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Dependencies install failed! Check your network and retry.
        echo Or run manually: "%PYEXE%" %PYARGS% -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
) else (
    echo [2/3] Dependencies OK.
)
echo.

rem ===== 3. Reuse an already healthy instance if one exists =====
powershell -NoProfile -ExecutionPolicy Bypass -Command "try{$r=Invoke-RestMethod -UseBasicParsing -Uri '%APP_URL%/health' -TimeoutSec 2;if($r.status -eq 'ok'){exit 0}}catch{};exit 1"
if not errorlevel 1 (
    echo [3/3] Service is already running.
    echo       URL:   %APP_URL%
    echo       Agent: %AGENT_URL%
    echo.
    if not "%NO_BROWSER%"=="1" (
        start "" "%APP_URL%"
        start "" "%AGENT_URL%"
    )
    exit /b 0
)

rem Port 5000 is expected to be free once the health check above fails.
netstat -ano | findstr ":5000 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [3/3] Port 5000 is occupied by another program.
    echo Close that program, then double-click this launcher again.
    echo.
    pause
    exit /b 1
)

echo [3/3] Starting server...
echo       URL:   %APP_URL%
echo       Agent: %AGENT_URL%
echo.
echo       Keep this window open = service running.
echo       Close this window = stop service.
echo       Press Ctrl+C to stop.
echo.

rem Open the browser only after the health endpoint reports that the app is ready.
if not "%NO_BROWSER%"=="1" (
    start "" /b powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "$u='%APP_URL%';$h=$u+'/health';$a='%AGENT_URL%';for($i=0;$i -lt 180;$i++){try{$r=Invoke-RestMethod -UseBasicParsing -Uri $h -TimeoutSec 2;if($r.status -eq 'ok'){Start-Process $u;Start-Process $a;exit}}catch{};Start-Sleep -Seconds 2}"
)

"%PYEXE%" %PYARGS% app.py
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo Service stopped. Press any key to close.
) else (
    echo Service exited with error code %EXIT_CODE%.
    echo Review the messages above, then press any key to close.
)
echo.
pause
exit /b %EXIT_CODE%

:NO_PYTHON
echo.
echo Python not found!
echo.
echo Please install Python 3.10+ and check "Add Python to PATH":
echo   https://www.python.org/downloads/
echo.
pause
exit /b 1
