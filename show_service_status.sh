#!/usr/bin/env bash
# Geekatplay ComfyUI Asset Vault - Geekatplay Studio - Vladimir Chopine
#
# Live service verification shared by both launchers (POSIX port of
# show_service_status.ps1).
#
# This deliberately queries the running API after startup instead of trusting a
# spawned process or an open port.  A listener can exist before the application
# is usable; these calls prove that the engine, UI, and background services are
# actually responding.
#
#   ./show_service_status.sh [port]
#
# Exits nonzero only when the API itself cannot be reached.

set -euo pipefail

PORT="${1:-8127}"
BASE="http://127.0.0.1:$PORT"

check() { printf '  [%s] %-12s %s\n' "$1" "$2" "$3"; }

if command -v curl >/dev/null 2>&1; then
    http_get() { curl -fsS --max-time 5 "$1"; }
elif command -v wget >/dev/null 2>&1; then
    http_get() { wget -q -T 5 -O - "$1"; }
else
    check FAIL 'Vault' 'Neither curl nor wget is installed; cannot query the API.'
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    check FAIL 'Vault' 'python3 was not found on PATH; cannot parse the API responses.'
    exit 1
fi

# jget FILE EXPR — evaluate a Python expression against parsed JSON in FILE.
jget() {
    python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
v=eval(sys.argv[2], {"d": d})
if isinstance(v, bool): v=str(v).lower()
print(v)' "$1" "$2" 2>/dev/null
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --------------------------------------------------------------- core probes
if ! http_get "$BASE/api/v1/ping" > "$TMP/ping.json" 2>/dev/null; then
    check FAIL 'Vault' "The API at $BASE/api/v1/ping did not respond."
    echo 'Live service verification failed.'
    exit 1
fi
if [ "$(jget "$TMP/ping.json" 'd.get("pong")')" != "true" ]; then
    check FAIL 'Vault' 'The ping endpoint did not return pong=true.'
    echo 'Live service verification failed.'
    exit 1
fi

if ! http_get "$BASE/" > "$TMP/page.html" 2>/dev/null; then
    check FAIL 'Vault' "The interface at $BASE/ did not return HTTP 200."
    echo 'Live service verification failed.'
    exit 1
fi

http_get "$BASE/api/v1/hash/status"       > "$TMP/hash.json"   2>/dev/null || : > "$TMP/hash.json"
http_get "$BASE/api/v1/index/status"      > "$TMP/index.json"  2>/dev/null || : > "$TMP/index.json"
http_get "$BASE/api/v1/embeddings/status" > "$TMP/embed.json"  2>/dev/null || : > "$TMP/embed.json"
http_get "$BASE/api/v1/system/health"     > "$TMP/health.json" 2>/dev/null || : > "$TMP/health.json"

echo
echo '  Live service report'
check OK 'Vault API' "port $PORT"
check OK 'Interface' "$BASE/ (HTTP 200)"

# ----------------------------------------------------------------- hash queue
HASHLINE="$(jget "$TMP/hash.json" '"configured %s; running %s; queued %s" % (d["concurrency"], d["queue"]["running"], d["queue"]["queued"])' || true)"
if [ -n "$HASHLINE" ]; then
    check OK 'Hash queue' "$HASHLINE"
else
    check WARN 'Hash queue' 'status endpoint did not answer'
fi

# -------------------------------------------------------------------- indexer
INDEX_ACTIVE="$(jget "$TMP/index.json" 'd.get("active")' || true)"
case "$INDEX_ACTIVE" in
    true)  check OK 'Indexer' 'scan active' ;;
    false) check OK 'Indexer' 'idle' ;;
    *)     check WARN 'Indexer' 'status endpoint did not answer' ;;
esac

# ----------------------------------------------------------------- embeddings
EMBED_STATE="$(jget "$TMP/embed.json" 'd.get("state")' || true)"
EMBED_PENDING="$(jget "$TMP/embed.json" 'd["index"]["pending"]' || true)"
if [ -n "$EMBED_STATE" ]; then
    if [ "$EMBED_STATE" = "ready" ]; then
        check OK 'Embeddings' "$EMBED_STATE; pending ${EMBED_PENDING:-?}"
    else
        check WARN 'Embeddings' "$EMBED_STATE; pending ${EMBED_PENDING:-?}"
    fi
else
    check WARN 'Embeddings' 'status endpoint did not answer'
fi

# --------------------------------------------------------------------- health
# One python3 pass renders every health check line plus the summary, matching
# show_service_status.ps1: asset findings are warnings, not runtime failures.
if [ -s "$TMP/health.json" ]; then
    python3 - "$TMP/health.json" <<'PYEOF'
import json, sys

with open(sys.argv[1]) as f:
    health = json.load(f)

ASSET_FINDINGS = {"integrity", "partial_downloads", "suspect_remotes"}

def line(state, name, message):
    print("  [%s] %-12s %s" % (state, name, message))

for check in health.get("checks", []):
    cid = check.get("id", "?")
    status = check.get("status", "")
    if cid in ASSET_FINDINGS and status != "ok":
        state = "WARN"
    else:
        state = {"ok": "OK", "warn": "WARN"}.get(status, "FAIL")
    detail = (check.get("message") or "").strip()
    if not detail and check.get("count", 0) > 0:
        examples = []
        for item in (check.get("items") or [])[:3]:
            examples.append(item.get("name") or item.get("path")
                            or item.get("package") or "item")
        detail = "%s item(s): %s" % (check["count"], ", ".join(examples))
    if not detail:
        detail = "ready"
    line(state, "Health/%s" % cid, detail)

required_failure = [c for c in health.get("checks", [])
                    if c.get("status") == "error"
                    and c.get("id") in ("comfyui_root", "database")]
if required_failure:
    line("FAIL", "Runtime health", "A required runtime service needs attention.")
elif health.get("status") != "ok":
    line("WARN", "Asset attention",
         "Runtime services are healthy; review the warnings above when convenient.")
else:
    line("OK", "Runtime health", "All required services are healthy.")
PYEOF
else
    check WARN 'Health' 'health endpoint did not answer'
fi

exit 0
