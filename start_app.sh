#!/usr/bin/env bash
# Geekatplay ComfyUI Asset Vault - Geekatplay Studio - Vladimir Chopine
#
# POSIX launcher. Behaves like start_app.bat: builds the interface if needed,
# starts the engine on 127.0.0.1:8127, waits until it accepts connections,
# verifies the live services, then opens the browser on that same port.
#
#   ./start_app.sh
#
# The engine keeps running after this script exits. Run ./stop_app.sh to stop it.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PORT=8127
PY="$ROOT/venv/bin/python"
LOG="$ROOT/backend_log.txt"

echo "==================================================================="
echo "    Geekatplay ComfyUI Asset Vault"
echo "    Vladimir Chopine"
echo "==================================================================="
echo

# ------------------------------------------------------------------- checks
if [ ! -x "$PY" ]; then
    echo "[ERROR] Python virtual environment not found at $ROOT/venv."
    echo "        Run ./install_dependencies.sh first."
    exit 1
fi

# Release archives ship a pre-built interface, so Node.js is only needed to
# build one from source.  With node_modules present (a dev checkout) the
# interface is always rebuilt so source edits are never served stale.
PREBUILT=""
if [ ! -d "$ROOT/frontend/node_modules" ] && [ -f "$ROOT/frontend/dist/index.html" ]; then
    PREBUILT=1
fi
if [ -z "$PREBUILT" ] && [ ! -d "$ROOT/frontend/node_modules" ]; then
    echo "[ERROR] No built interface at $ROOT/frontend/dist and no frontend"
    echo "        dependencies at $ROOT/frontend/node_modules."
    echo "        Run ./install_dependencies.sh first, or use a release archive"
    echo "        that ships the interface pre-built."
    exit 1
fi

# Pick an HTTP probe tool once; everything below uses http_get URL.
if command -v curl >/dev/null 2>&1; then
    http_get() { curl -fsS --max-time 5 "$1"; }
elif command -v wget >/dev/null 2>&1; then
    http_get() { wget -q -T 5 -O - "$1"; }
else
    echo "[ERROR] Neither curl nor wget is installed. Install curl and try again."
    exit 1
fi

# ------------------------------------------------------- port in use test
if "$PY" -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1',$PORT))==0 else 1)" >/dev/null 2>&1; then
    echo "[ERROR] Port $PORT is already in use."
    echo "        Another copy of the Asset Vault may already be running."
    echo "        Close it, or run ./stop_app.sh, then try again."
    exit 1
fi

# ---------------------------------------------------------- build interface
# Serve the production build from the engine.  This keeps hashing independent
# of the Vite development server, so closing/reloading the UI cannot stop it.
if [ -n "$PREBUILT" ]; then
    echo "[1/3] Interface build already present - reusing frontend/dist."
else
    echo "[1/3] Building the interface ..."
    if ! command -v npm >/dev/null 2>&1; then
        echo "[ERROR] npm was not found on PATH. Install Node.js 18 or newer."
        exit 1
    fi
    if ! (cd "$ROOT/frontend" && npm run build); then
        echo "[ERROR] The interface build failed. Fix the errors above and try again."
        exit 1
    fi
fi

# ------------------------------------------------------------------ backend
echo "[2/3] Starting the vault engine on http://127.0.0.1:$PORT ..."
nohup "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --app-dir backend > "$LOG" 2>&1 &
ENGINE_PID=$!

# --------------------------------------------- wait until it really listens
echo "[3/3] Waiting for the engine to accept connections ..."
READY=
for i in $(seq 1 45); do
    if http_get "http://127.0.0.1:$PORT/api/v1/ping" >/dev/null 2>&1; then
        READY=1
        break
    fi
    if ! kill -0 "$ENGINE_PID" 2>/dev/null; then
        break
    fi
    sleep 1
done

if [ -z "$READY" ]; then
    echo
    echo "[ERROR] The engine did not start within 45 seconds."
    echo "        Look at backend_log.txt for the reason. Last lines:"
    echo "---------------------------------------------------------------"
    [ -f "$LOG" ] && tail -n 20 "$LOG"
    echo "---------------------------------------------------------------"
    "$ROOT/stop_app.sh" --quiet || true
    exit 1
fi
echo "      Engine is up."

# ------------------------------------------------------- live verification
if ! "$ROOT/show_service_status.sh" "$PORT"; then
    echo "[ERROR] The engine opened its port but failed a live service check."
    echo "        See backend_log.txt for details."
    "$ROOT/stop_app.sh" --quiet || true
    exit 1
fi

# ----------------------------------------------------------------- interface
echo
echo "==================================================================="
echo "  Asset Vault is running independently of this launcher window."
echo "    Interface : http://127.0.0.1:$PORT/"
echo "    API docs  : http://127.0.0.1:$PORT/docs"
echo
echo "  Close this window freely. Run ./stop_app.sh only when you want to stop the vault."
echo "==================================================================="
echo

# The wait loop above guarantees the API answers before this.
case "$(uname)" in
    Darwin) open "http://127.0.0.1:$PORT/" >/dev/null 2>&1 || true ;;
    *)
        if command -v xdg-open >/dev/null 2>&1; then
            xdg-open "http://127.0.0.1:$PORT/" >/dev/null 2>&1 || true
        else
            echo "  Open http://127.0.0.1:$PORT/ in your browser."
        fi
        ;;
esac
