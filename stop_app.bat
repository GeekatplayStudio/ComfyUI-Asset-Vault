@echo off
setlocal EnableDelayedExpansion
set "ROOT=%~dp0"
set "QUIET="
if /I "%~1"=="--quiet" set "QUIET=1"

if not defined QUIET TITLE Geekatplay ComfyUI Asset Vault - Stop

set "PORT=8127"

REM The clean stop lives in stop_app.ps1: the engine is asked to shut down, folds
REM its database log, and exits on its own.  A forced stop skips that fold, and
REM the next start then has to do it first - minutes on a hard disk.
if defined QUIET (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%stop_app.ps1" -Port %PORT% -Quiet
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%stop_app.ps1" -Port %PORT%
)
if !errorlevel! equ 0 goto done

REM PowerShell could not run the script: fall back to ending the listener by force.
set "STOPPED="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:"LISTENING" ^| findstr /C:":%PORT% "') do (
    if not "%%p"=="0" (
        taskkill /PID %%p /F >nul 2>&1
        if !errorlevel! equ 0 (
            set "STOPPED=1"
            if not defined QUIET echo   Stopped the vault engine ^(pid %%p^) on port %PORT%.
        )
    )
)
if not defined STOPPED (
    if not defined QUIET echo   Nothing was listening on port %PORT%.
)

:done
if not defined QUIET (
    pause
)
endlocal
