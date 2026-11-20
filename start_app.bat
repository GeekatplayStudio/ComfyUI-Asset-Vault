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
REM First run: nothing is installed yet, so install it now.  This makes
REM double-clicking start_app.bat the only step a new user needs.
if not exist "venv\Scripts\python.exe" (
    echo First run detected - installing everything the vault needs.
    echo This takes a few minutes and only happens once.
    echo.
    call "%ROOT%install_dependencies.bat" --auto
    if errorlevel 1 (
        echo.
        echo [ERROR] The automatic install did not finish. Scroll up for the reason.
        echo.
        pause
        exit /b 1
    )
    echo.
)
if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Python virtual environment not found at "%ROOT%venv".
    echo         Run install_dependencies.bat and read what it says.
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

REM ----------------------------------------------- apply a staged update
REM Nothing is running yet and nothing is imported, which is the only safe
REM moment to replace the app's own files.  Exits 0 when nothing is staged.
if exist "%ROOT%backend\data\updates\pending.json" (
    echo [0/3] Applying the downloaded update ...
    "%ROOT%venv\Scripts\python.exe" "%ROOT%apply_update.py"
    if errorlevel 2 (
        echo [ERROR] The update failed and could not be rolled back.
        echo         Your previous files are in backend\data\updates\backup.
        echo.
        pause
        exit /b 1
    )
    if errorlevel 1 (
        echo [WARN]  The update failed and was rolled back. Continuing on the
        echo         current version.
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
REM An engine from an earlier launch may still be getting ready (not listening
REM yet, so the port test above passed).  Starting a second one on the same
REM database would only make both slower: wait for that one instead.
call :engine_running
if !errorlevel! equ 0 (
    echo [2/3] A vault engine from an earlier launch is still getting ready.
    echo       Waiting for it instead of starting a second one.
) else (
    echo [2/3] Starting the vault engine on http://127.0.0.1:%PORT% ...
    start "Geekatplay Vault Engine" /min cmd /c ""%ROOT%venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% --app-dir backend > "%ROOT%backend_log.txt" 2>&1"
)

REM ------------------------------------------- wait until it really listens
REM No fixed deadline while the engine is alive: after an upgrade, or after it
REM was not stopped cleanly, it first folds its database log, which on a hard
REM disk takes minutes.  Giving up at 45 seconds reported a working engine as
REM failed and left it running with nobody waiting for it.
echo [3/3] Waiting for the engine to accept connections ...
set /a WAITED=0
set /a MAX_WAIT=3600
:wait_loop
"%ROOT%venv\Scripts\python.exe" -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1',%PORT%))==0 else 1)" >nul 2>&1
if !errorlevel! equ 0 goto engine_up
set /a WAITED+=1
set /a TICK=WAITED %% 15
if !TICK! equ 0 (
    call :engine_running
    if errorlevel 1 goto engine_died
    if !WAITED! equ 15 (
        echo       The engine is still getting ready. After an upgrade, or when it was
        echo       not stopped cleanly, this can take several minutes on a hard disk.
        echo       Leave this window open - the browser opens when it is ready.
    )
    call :show_progress
)
if !WAITED! geq !MAX_WAIT! goto engine_slow
timeout /t 1 /nobreak >nul
goto wait_loop

:engine_died
echo.
echo [ERROR] The engine stopped before it was ready.
echo         Look at backend_log.txt for the reason. Last lines:
echo ---------------------------------------------------------------
if exist "%ROOT%backend_log.txt" powershell -NoProfile -Command "Get-Content '%ROOT%backend_log.txt' -Tail 20"
echo ---------------------------------------------------------------
echo.
pause
exit /b 1

:engine_slow
echo.
echo [WARN]  The engine is still working after an hour and has not opened its port.
echo         It has been left running. Open http://127.0.0.1:%PORT%/ later, or run
echo         stop_app.bat to stop it. Last lines of backend_log.txt:
echo ---------------------------------------------------------------
if exist "%ROOT%backend_log.txt" powershell -NoProfile -Command "Get-Content '%ROOT%backend_log.txt' -Tail 20"
echo ---------------------------------------------------------------
echo.
pause
exit /b 1

:engine_up
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
exit /b 0

REM ------------------------------------------------------------ helpers
REM Exit code 0 when a vault engine process for this port exists, 1 otherwise.
REM Matched on the command line, so it also finds one this window did not start.
:engine_running
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*app.main:app*--port %PORT%*' }; if ($p) { exit 0 } else { exit 1 }" >nul 2>&1
exit /b %errorlevel%

REM One line of what the engine is doing, from the end of its log.
:show_progress
powershell -NoProfile -Command "$f = '%ROOT%backend_log.txt'; $l = ''; if (Test-Path -LiteralPath $f) { $l = Get-Content -LiteralPath $f -Tail 1 }; Write-Host ('      still starting (%WAITED% s) ' + $l)"
exit /b 0
