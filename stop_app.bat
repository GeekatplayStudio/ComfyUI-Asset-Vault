@echo off
setlocal EnableDelayedExpansion
set "QUIET="
if /I "%~1"=="--quiet" set "QUIET=1"

if not defined QUIET (
    TITLE Geekatplay ComfyUI Asset Vault - Stop
    echo ===================================================================
    echo     Stopping Geekatplay ComfyUI Asset Vault
    echo ===================================================================
    echo.
)

set "PORT=8127"
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

if not defined QUIET (
    echo.
    echo   Note: the interface runs in its own window. Close that window,
    echo   or press Ctrl+C in it, to stop the dev server.
    echo.
    pause
)
endlocal
