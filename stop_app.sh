#!/usr/bin/env bash
# Geekatplay ComfyUI Asset Vault - Geekatplay Studio - Vladimir Chopine
#
# POSIX equivalent of stop_app.bat: finds whatever is listening on port 8127
# and stops it.
#
#   ./stop_app.sh [--quiet]

set -euo pipefail

PORT=8127
QUIET=
[ "${1:-}" = "--quiet" ] && QUIET=1

if [ -z "$QUIET" ]; then
    echo "==================================================================="
    echo "    Stopping Geekatplay ComfyUI Asset Vault"
    echo "==================================================================="
    echo
fi

# Collect PIDs listening on the port, using whichever tool is available.
PIDS=""
if command -v lsof >/dev/null 2>&1; then
    PIDS="$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null || true)"
elif command -v fuser >/dev/null 2>&1; then
    PIDS="$(fuser "$PORT/tcp" 2>/dev/null | tr -s ' ' '\n' || true)"
elif command -v ss >/dev/null 2>&1; then
    PIDS="$(ss -ltnp 2>/dev/null | grep ":$PORT " | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u || true)"
else
    echo "[ERROR] None of lsof, fuser, or ss is installed; cannot find the listener on port $PORT."
    echo "        Install lsof and try again."
    exit 1
fi

STOPPED=
# SIGTERM is uvicorn's clean shutdown: running work stops and the database log
# is folded back into the database before the process exits.  On a hard disk a
# large log takes minutes, so the grace period is generous; escalating early
# would skip the fold and hand the work to the next start.
GRACE=300
for pid in $PIDS; do
    [ -n "$pid" ] || continue
    if kill "$pid" 2>/dev/null; then
        STOPPED=1
        [ -z "$QUIET" ] && echo "  Asked the vault engine (pid $pid) on port $PORT to shut down cleanly ..."
        i=0
        while [ "$i" -lt "$GRACE" ] && kill -0 "$pid" 2>/dev/null; do
            i=$((i + 1))
            if [ -z "$QUIET" ] && [ $((i % 10)) -eq 0 ]; then
                echo "  Still finishing up (saving the database) ..."
            fi
            sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
            [ -z "$QUIET" ] && echo "  It did not exit on its own; stopping it by force."
            kill -9 "$pid" 2>/dev/null || true
        fi
        [ -z "$QUIET" ] && echo "  Stopped the vault engine (pid $pid) on port $PORT."
    fi
done

if [ -z "$STOPPED" ] && [ -z "$QUIET" ]; then
    echo "  Nothing was listening on port $PORT."
fi

if [ -z "$QUIET" ]; then
    echo
    echo "  Note: close or refresh the browser tab after stopping the vault."
    echo
fi
