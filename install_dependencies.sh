#!/usr/bin/env bash
# Geekatplay ComfyUI Asset Vault - Geekatplay Studio - Vladimir Chopine
#
# POSIX dependency installer, identical in effect to install_dependencies.bat.
#
#   ./install_dependencies.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PY="$ROOT/venv/bin/python"

echo "==================================================================="
echo "    Geekatplay ComfyUI Asset Vault"
echo "    Vladimir Chopine - dependency installer"
echo "==================================================================="
echo

# ------------------------------------------------------------- [1/5] Python
echo "[1/5] Looking for Python 3.10 or newer ..."
if ! command -v python3 >/dev/null 2>&1; then
    echo
    echo "[ERROR] python3 was not found on PATH."
    echo "        Install Python 3.12 with your package manager"
    echo "        (e.g. apt install python3 python3-venv, brew install python@3.12)"
    echo "        and run this installer again."
    exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
    echo
    echo "[ERROR] The Python on PATH is too old. This app needs 3.10 or newer;"
    echo "        3.12 is what it is developed and tested against."
    python3 --version || true
    exit 1
fi
echo "      Found $(python3 --version 2>&1)"

# --------------------------------------------------------- [2/5] virtualenv
echo "[2/5] Preparing the virtual environment in $ROOT/venv ..."
if [ -x "$PY" ]; then
    echo "      Already present - reusing it."
else
    if ! python3 -m venv "$ROOT/venv"; then
        echo
        echo "[ERROR] Could not create the virtual environment."
        echo "        On Debian/Ubuntu you may need: apt install python3-venv"
        exit 1
    fi
    echo "      Created."
fi

if [ ! -x "$PY" ]; then
    echo
    echo "[ERROR] $PY is missing even though the environment was created."
    echo "        Delete the venv folder and run this installer again."
    exit 1
fi

# ---------------------------------------------------- [3/5] backend packages
echo "[3/5] Installing the engine's Python packages ..."
if ! "$PY" -m pip install --upgrade pip --disable-pip-version-check -q; then
    echo "[WARN]  pip could not upgrade itself. Continuing with the current version."
fi

if ! "$PY" -m pip install -r "$ROOT/backend/requirements.txt" --disable-pip-version-check; then
    echo
    echo "[ERROR] Installing the Python packages failed. Scroll up for the reason."
    echo "        The usual causes are no internet connection or a proxy that"
    echo "        blocks pypi.org."
    exit 1
fi

if ! "$PY" -c 'import fastapi, uvicorn, pydantic, httpx, PIL, numpy, yaml, onnxruntime, tokenizers' >/dev/null 2>&1; then
    echo
    echo "[ERROR] The packages installed but cannot all be imported."
    echo "        Delete the venv folder and run this installer again."
    exit 1
fi
echo "      Engine packages verified."

# --------------------------------------------------- [4/5] frontend packages
echo "[4/5] Installing the interface's Node packages ..."
NODE_OK=
if ! command -v npm >/dev/null 2>&1; then
    if [ -f "$ROOT/frontend/dist/index.html" ]; then
        echo "      Node.js is not installed, and it is not needed: this archive"
        echo "      ships the interface pre-built at frontend/dist. The engine"
        echo "      serves it directly at http://127.0.0.1:8127/."
    else
        echo
        echo "[WARN]  Node.js was not found on PATH, so the interface was skipped."
        echo "        Install Node.js 18 or newer from https://nodejs.org and run"
        echo "        this installer again. The engine and its API already work."
        echo
    fi
else
    NODE_VERSION="$(node --version 2>/dev/null || echo v0)"
    NODE_MAJOR="${NODE_VERSION#v}"
    NODE_MAJOR="${NODE_MAJOR%%.*}"
    if [ "${NODE_MAJOR:-0}" -lt 18 ] 2>/dev/null; then
        echo
        echo "[WARN]  Node.js $NODE_VERSION is too old; this app needs Node 18 or newer."
        echo "        Install a newer Node.js from https://nodejs.org and run"
        echo "        this installer again. The engine and its API already work."
        echo
    else
        echo "      Node $NODE_VERSION"
        NODE_OK=1
    fi
fi

if [ -n "$NODE_OK" ]; then
    if ! (cd "$ROOT/frontend" && npm install); then
        echo
        echo "[ERROR] npm install failed. Scroll up for the reason."
        exit 1
    fi

    # ------------------------------------------------------ [5/5] SPA build
    echo "[5/5] Building the interface ..."
    if ! (cd "$ROOT/frontend" && npm run build); then
        echo "[WARN]  The production build failed. start_app.sh will try to"
        echo "        build the interface again before launching."
    else
        echo "      Built. The engine can now also serve the interface directly"
        echo "      at http://127.0.0.1:8127/ without a development server."
    fi
fi

echo
echo "==================================================================="
echo "    Installation finished."
echo "    Launch the app with ./start_app.sh"
echo "==================================================================="
echo
