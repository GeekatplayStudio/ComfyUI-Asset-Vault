@echo off
setlocal EnableDelayedExpansion
TITLE Geekatplay ComfyUI Asset Vault
COLOR 0E

set "ROOT=%~dp0"
cd /d "%ROOT%"

set "PORT=8127"

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

REM Release archives ship a pre-built interface, so Node.js is only needed to
REM build one from source.  With node_modules present (a dev checkout) the
REM interface is always rebuilt so source edits are never served stale.
set "PREBUILT="
if not exist "frontend\node_modules" if exist "frontend\dist\index.html" set "PREBUILT=1"

if not defined PREBUILT (
    if not exist "frontend\node_modules" (
        echo [ERROR] No built interface at "%ROOT%frontend\dist" and no frontend
        echo         dependencies at "%ROOT%frontend\node_modules".
        echo         Run install_dependencies.bat first, or use a release archive
        echo         that ships the interface pre-built.
        echo.
        pause
        exit /b 1
    )
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

REM ---------------------------------------------------------- build interface
REM Serve the production build from the engine.  This keeps hashing independent
REM of the Vite development server, so closing/reloading the UI cannot stop it.
if defined PREBUILT (
    echo [1/3] Interface build already present - reusing frontend\dist
) else (
    echo [1/3] Building the interface ...
    pushd "%ROOT%frontend"
    call npm run build
    if errorlevel 1 (
        popd
        echo [ERROR] The interface build failed. Fix the errors above and try again.
        echo.
        pause
        exit /b 1
    )
    popd
)

REM ------------------------------------------------------------ backend
echo [2/3] Starting the vault engine on http://127.0.0.1:%PORT% ...
start "Geekatplay Vault Engine" /min cmd /c ""%ROOT%venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% --app-dir backend > "%ROOT%backend_log.txt" 2>&1"

REM ------------------------------------------- wait until it really listens
echo [3/3] Waiting for the engine to accept connections ...
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

REM ------------------------------------------------------- live verification
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%show_service_status.ps1" -Port %PORT%
if errorlevel 1 (
    echo [ERROR] The engine opened its port but failed a live service check.
    echo         See backend_log.txt for details.
    call "%ROOT%stop_app.bat" --quiet
    pause
    exit /b 1
)

REM ----------------------------------------------------------- interface
echo.
echo ===================================================================
echo   Asset Vault is running independently of this launcher window.
echo     Interface : http://127.0.0.1:%PORT%/
echo     API docs  : http://127.0.0.1:%PORT%/docs
echo.
echo   Close this window freely. Run stop_app.bat only when you want to stop the vault.
echo ===================================================================
echo.

REM The wait loop above guarantees the API answers before this.
start "" "http://127.0.0.1:%PORT%/"
endlocal
