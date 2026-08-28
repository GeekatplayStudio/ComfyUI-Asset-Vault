@echo off
setlocal EnableDelayedExpansion
TITLE Geekatplay ComfyUI Asset Vault - Dependency Installer
COLOR 0B

set "ROOT=%~dp0"
cd /d "%ROOT%"

set "PY=%ROOT%venv\Scripts\python.exe"

echo ===================================================================
echo     Geekatplay ComfyUI Asset Vault
echo     Vladimir Chopine - dependency installer
echo ===================================================================
echo.

REM ------------------------------------------------------------ [1/5] Python
echo [1/5] Looking for Python 3.11 or newer ...
where python >nul 2>&1
if !errorlevel! neq 0 (
    echo.
    echo [ERROR] Python was not found on PATH.
    echo         Install Python 3.12 from https://www.python.org/downloads/windows/
    echo         and tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if !errorlevel! neq 0 (
    echo.
    echo [ERROR] The Python on PATH is too old. This app needs 3.11 or newer;
    echo         3.12 is what it is developed and tested against.
    python --version
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version 2^>^&1') do echo       Found %%v

REM ------------------------------------------------------ [2/5] virtualenv
echo [2/5] Preparing the virtual environment in "%ROOT%venv" ...
if exist "%PY%" (
    echo       Already present - reusing it.
) else (
    python -m venv "%ROOT%venv"
    if !errorlevel! neq 0 (
        echo.
        echo [ERROR] Could not create the virtual environment.
        echo.
        pause
        exit /b 1
    )
    echo       Created.
)

if not exist "%PY%" (
    echo.
    echo [ERROR] "%PY%" is missing even though the environment was created.
    echo         Delete the venv folder and run this installer again.
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------- [3/5] backend packages
echo [3/5] Installing the engine's Python packages ...
"%PY%" -m pip install --upgrade pip --disable-pip-version-check -q
if !errorlevel! neq 0 (
    echo [WARN]  pip could not upgrade itself. Continuing with the current version.
)

"%PY%" -m pip install -r "%ROOT%backend\requirements.txt" --disable-pip-version-check
if !errorlevel! neq 0 (
    echo.
    echo [ERROR] Installing the Python packages failed. Scroll up for the reason.
    echo         The usual causes are no internet connection or a proxy that
    echo         blocks pypi.org.
    echo.
    pause
    exit /b 1
)

"%PY%" -c "import fastapi, uvicorn, pydantic, httpx, PIL, numpy, yaml, onnxruntime, tokenizers" >nul 2>&1
if !errorlevel! neq 0 (
    echo.
    echo [ERROR] The packages installed but cannot all be imported.
    echo         Delete the venv folder and run this installer again.
    echo.
    pause
    exit /b 1
)
echo       Engine packages verified.

REM ------------------------------------------------ [4/5] frontend packages
echo [4/5] Installing the interface's Node packages ...
where npm >nul 2>&1
if !errorlevel! neq 0 (
    if exist "%ROOT%frontend\dist\index.html" (
        echo       Node.js is not installed, and it is not needed: this archive
        echo       ships the interface pre-built at frontend\dist. The engine
        echo       serves it directly at http://127.0.0.1:8127/.
        goto :done
    )
    echo.
    echo [WARN]  Node.js was not found on PATH, so the interface was skipped.
    echo         Install Node.js 18 or newer from https://nodejs.org and run
    echo         this installer again. The engine and its API already work.
    echo.
    goto :done
)
for /f "delims=" %%v in ('node --version 2^>^&1') do echo       Node %%v

pushd "%ROOT%frontend"
call npm install
if !errorlevel! neq 0 (
    popd
    echo.
    echo [ERROR] npm install failed. Scroll up for the reason.
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------------- [5/5] SPA build
echo [5/5] Building the interface ...
call npm run build
if !errorlevel! neq 0 (
    echo [WARN]  The production build failed. start_app.bat will try to build
    echo         the interface again before launching.
) else (
    echo       Built. The engine can now also serve the interface directly
    echo       at http://127.0.0.1:8127/ without a development server.
)
popd

:done
echo.
echo ===================================================================
echo     Installation finished.
echo     Launch the app with start_app.bat
echo ===================================================================
echo.
pause
endlocal
