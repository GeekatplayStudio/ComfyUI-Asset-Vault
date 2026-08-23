"""``python -m app.mcp_stdio`` - the stdio MCP entrypoint.

Order matters in this file and nowhere else in the project:

1. grab the REAL stdout handle,
2. force every logger onto stderr,
3. re-point ``sys.stdout`` at stderr so a stray ``print`` anywhere in the
   import graph cannot corrupt the JSON-RPC stream,
4. only then import anything from the app.

Client configuration (README):

```json
{ "mcpServers": {
    "geekatplay-vault": {
      "command": "D:\\\\Projects\\\\ComfyUIAssetManager\\\\venv\\\\Scripts\\\\python.exe",
      "args": ["-m","app.mcp_stdio"],
      "cwd": "D:\\\\Projects\\\\ComfyUIAssetManager\\\\backend",
      "env": { "VAULT_DB": "D:\\\\Projects\\\\ComfyUIAssetManager\\\\backend\\\\data\\\\vault.db" }
} } }
```
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import sys

# --- 1/2/3: stdout discipline, before any app import -------------------------
_RAW_STDOUT = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else None
REAL_STDOUT = (io.TextIOWrapper(_RAW_STDOUT, encoding="utf-8", newline="\n",
                                write_through=True)
               if _RAW_STDOUT is not None else sys.stdout)

logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("VAULT_LOG_LEVEL", "WARNING").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    force=True,
)

sys.stdout = sys.stderr  # any stray print() lands on stderr, never on the wire

# stdin must be UTF-8 for non-ASCII / CJK / emoji filenames
with contextlib.suppress(AttributeError, ValueError):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    db_path = os.environ.get("VAULT_DB")
    if db_path:
        from .core import db as dbmod

        dbmod.set_db_path(db_path)

    from .mcp import stdio

    try:
        return stdio.serve(argv, stdin=sys.stdin, stdout=REAL_STDOUT)
    finally:
        stdio.shutdown()


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
