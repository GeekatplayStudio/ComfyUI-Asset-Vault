@echo off
setlocal EnableDelayedExpansion
TITLE Geekatplay ComfyUI Asset Vault
COLOR 0E

set "ROOT=%~dp0"
cd /d "%ROOT%"

set "PORT=8127"
set "UI_PORT=3000"

echo ===================================================================
echo     Geekatplay ComfyUI Asset Vault
echo     Vladimir Chopine
echo ===================================================================
echo.

REM ---------------------------------------------------------------- checks
if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Python virtual environment not found at "%ROOT%venv".
    echo         Run install_dependencies.bat first.
    echo.
    pause
    exit /b 1
)

if not exist "frontend\node_modules" (
    echo [ERROR] Frontend dependencies not installed at "%ROOT%frontend\node_modules".
    echo         Run install_dependencies.bat first.
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------------- port in use test
netstat -ano | findstr /R /C:"LISTENING" | findstr /C:":%PORT% " >nul 2>&1
if !errorlevel! equ 0 (
    echo [ERROR] Port %PORT% is already in use.
    echo         Another copy of the Asset Vault may already be running.
    echo         Close it, or run stop_app.bat, then try again.
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------------------ backend
echo [1/3] Starting the vault engine on http://127.0.0.1:%PORT% ...
start "Geekatplay Vault Engine" /min cmd /c ""%ROOT%venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% --app-dir backend > "%ROOT%backend_log.txt" 2>&1"

REM ------------------------------------------- wait until it really listens
echo [2/3] Waiting for the engine to accept connections ...
set "READY="
for /L %%i in (1,1,45) do (
    if not defined READY (
        "%ROOT%venv\Scripts\python.exe" -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1',%PORT%))==0 else 1)" >nul 2>&1
        if !errorlevel! equ 0 (
            set "READY=1"
        ) else (
            timeout /t 1 /nobreak >nul
        )
    )
)

if not defined READY (
    echo.
    echo [ERROR] The engine did not start within 45 seconds.
    echo         Look at backend_log.txt for the reason. Last lines:
    echo ---------------------------------------------------------------
    if exist "%ROOT%backend_log.txt" powershell -NoProfile -Command "Get-Content '%ROOT%backend_log.txt' -Tail 20"
    echo ---------------------------------------------------------------
    echo.
    pause
    exit /b 1
)

echo       Engine is up.

REM ----------------------------------------------------------- frontend
echo [3/3] Starting the interface on http://localhost:%UI_PORT% ...
echo.
echo ===================================================================
echo   Asset Vault is running.
echo     Interface : http://localhost:%UI_PORT%
echo     API docs  : http://127.0.0.1:%PORT%/docs
echo.
echo   Close this window or run stop_app.bat to shut it down.
echo ===================================================================
echo.

REM Unquoted, so cmd treats the URL as the document to open rather than as the
REM window title. The wait loop above guarantees the API answers before this.
start http://localhost:%UI_PORT%
cd /d "%ROOT%frontend"
call npm run dev

REM npm run dev holds the window; when it exits, stop the engine too.
echo.
echo Interface stopped. Shutting the engine down ...
call "%ROOT%stop_app.bat" --quiet
endlocal
