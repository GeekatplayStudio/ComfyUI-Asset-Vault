# Installation

Geekatplay ComfyUI Asset Vault · **Geekatplay Studio — Vladimir Chopine**

---

## 1. What you need

| | Minimum | Verified on |
|---|---|---|
| Windows | 10 or 11, 64-bit | Windows 11 Pro 26200 |
| Python on PATH | 3.11 | **3.12.10** |
| Node.js on PATH | 18 — **source checkouts only**; a release archive ships the interface pre-built and never needs Node | **22.22.0** (npm 10.9.4) |
| A ComfyUI installation | any | **ComfyUI 0.33.0** portable, frontend package 1.49.6 |

Linux and macOS are **experimental**: use `./install_dependencies.sh` and `./start_app.sh` in
place of the `.bat` files below. The engine, indexing, search and file operations are
platform-neutral; the ComfyUI start/update integration still expects the Windows portable
layout, and the platform test suites have not yet been run there.

Free space for the vault's own data — the database, its write-ahead log and the thumbnail cache —
budget **2–3 GB** for a library the size of the reference install. The thumbnail cache has its own
cap, 2048 MB by default, adjustable in Settings.

The app never writes into your ComfyUI folder except when you explicitly ask it to (a rename, a
move, or a delete, which lands in `.vault-trash` inside that root).

If Python or Node is missing:

* Python — <https://www.python.org/downloads/windows/>, tick **Add python.exe to PATH**.
* Node.js — <https://nodejs.org>, the LTS installer adds itself to PATH.

Both need an internet connection **once**, to download packages. After that the app runs fully
offline.

---

## 2. Install

Double-click, or from a command prompt in the project folder:

```bat
install_dependencies.bat
```

PowerShell users can run the equivalent script instead:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_dependencies.ps1
```

The installer:

1. checks that the Python on PATH is 3.11 or newer,
2. creates `venv\` if it is not already there,
3. installs `backend\requirements.txt` into it and then **verifies every package imports**,
4. runs `npm install` in `frontend\`,
5. runs `npm run build`, which produces `frontend\dist` so the engine can serve the interface by
   itself later.

Any failure stops the installer with an explanation rather than continuing into a broken state.
Steps 4–5 are skipped when Node is missing: with a release archive that is normal and silent
(the interface ships pre-built in `frontend\dist`), and with a source checkout it is a warning —
the engine and its API still work; only *building* the interface needs Node.

### Doing it by hand

```bat
python -m venv venv
venv\Scripts\python.exe -m pip install -r backend\requirements.txt
cd frontend
npm install
npm run build
```

### What gets installed

Engine: `fastapi`, `uvicorn[standard]`, `pydantic`, `httpx`, `pillow`, `numpy`, `PyYAML`,
`onnxruntime`, `tokenizers`, plus `pytest`, `pytest-asyncio` and `ruff` for the test suite.
There is deliberately **no torch** — the optional semantic search runs on CPU through ONNX
Runtime.

Interface: React 18, Vite, `lucide-react`. Nothing else.

---

## 3. Start it

```bat
start_app.bat
```

or

```powershell
powershell -ExecutionPolicy Bypass -File .\start_app.ps1
```

What the launcher does, in order:

0. applies any staged self-update via `apply_update.py` before starting the engine,
1. refuses to continue if **both** `venv\` and a usable interface (`frontend\node_modules`
   or a pre-built `frontend\dist`) are missing,
2. refuses to continue if something is already listening on **8127**,
3. starts the engine — `uvicorn app.main:app --host 127.0.0.1 --port 8127 --app-dir backend` —
   with all output going to `backend_log.txt`,
4. polls the port for up to 45 seconds and **only then** opens the browser; if the engine never
   comes up it prints the last 20 lines of `backend_log.txt` and stops,
5. builds the interface, serves it from the engine on **http://127.0.0.1:8127/**,
   and opens it. The hashing service keeps running if the browser or launcher closes.

Before opening the browser, the launcher prints a **Live service report** from the running API:
the engine PID, interface response, hash queue/workers, indexer, embeddings, and every health
check. A port opening alone is not treated as a successful startup.

On the reference machine the engine answers `/api/v1/ping` about **one second** after launch.

Closing the launcher window does **not** shut the engine down. Use `stop_app.bat` when you
explicitly want to stop the vault; queued hashing resumes safely after a restart.

### Two ways to open the interface

| | URL | When |
|---|---|---|
| Served by the engine | `http://127.0.0.1:8127/` | What `start_app.bat` opens. One process, one port, no Node needed at run time. |
| Development server | `http://localhost:3000` | Optional for development: run `cd frontend && npm run dev`. It proxies `/api` to the engine. |

Both talk to the same engine and the same database. The interactive API reference is always at
`http://127.0.0.1:8127/docs`.

---

## 4. First launch

The first time the interface loads it shows a short wizard.

1. **Point it at ComfyUI.** Type or paste the installation folder — the one that contains
   `models\`, `custom_nodes\` and `output\`. It is validated live: the wizard tells you whether
   the folder exists, whether it looks like a ComfyUI install, and which sub-folders it found,
   *before* you can save.
2. **Confirm.** Extra roots declared in `extra_model_paths.yaml` are picked up automatically.
   `extra_model_paths.yaml.hold` is ignored unless you turn that on in Settings.
3. **The first scan runs.** On the reference install a cold full scan takes **13.8–17.4 seconds**
   and finds 237 models, 34 node packages, 1,866 node classes, 211 workflows and 3,834 outputs.
   Later scans are incremental and finish in **0.31–0.37 s**.

Nothing is hashed, downloaded or contacted online during this. The first scan reads only
safetensors/GGUF headers and filesystem metadata.

### Changing the ComfyUI folder later

Settings → **Location**. The same live validation applies, and you are offered a re-index
immediately. Existing rows for the old folder are **retained, not deleted** — the old root is
marked retired, its models, workflows and outputs keep their ratings, tags, notes and album
membership, and the missing-file sweep skips them so nothing is flagged just because a drive was
unplugged. Retired roots are read-only: rename, move and delete are refused for them. Remove that
content explicitly from the Storage tab when you are sure you want it gone.

---

## 5. Optional features, all off by default

### Hashing — required before any Civitai data appears

Civitai identifies a file by **AutoV2**, the first 10 hex characters of its full-file SHA-256.
Computing that means reading every byte, so it is opt-in, runs in the background, and is
cancellable and resumable. Results are cached against `(path, size, mtime)` and survive restarts.

Start it from the **Hash** button in the top bar and choose a scope — everything not hashed yet,
every model file, one category, or just the models you have selected. A model card is fully
usable before its hash exists; the Civitai fields simply fill in later.

**Be realistic about the cost.** Hashing the full 1.5 TB reference library is roughly a
**2.8-hour** job, disk-bound. Hash one category first if you only care about, say, your LoRAs.

The same dialog carries the read concurrency and an MB/s throttle, so you can leave it running
while you work, and shows the live queue with a cancel button.

### Civitai enrichment

Settings → Search. Turn on **Allow outbound lookups at all**, then **Match hashed files against
Civitai**. Only a hash goes out — never a file, never a prompt. Descriptions, trigger words,
recommended sampler/CFG/steps and update alerts appear for models that have been hashed and
matched. Offline, the app degrades silently.

### Smart search — the local ONNX embedding model

Lexical search always works. **Smart** adds semantic ranking, fused with the lexical results.
It needs a one-time **~23 MB** download of `all-MiniLM-L6-v2` INT8 (384 dimensions, CPU only).

Settings → Search → **Enable smart search**. Progress is shown; embeddings are then computed
incrementally in the background for all 6,182 documents.

**Offline / manual placement.** If the machine cannot reach Hugging Face, place three files into

```
backend\data\models\all-MiniLM-L6-v2\
```

| Save as | Fetch from `https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/` |
|---|---|
| `model.onnx` | `onnx/model_quantized.onnx` |
| `tokenizer.json` | `tokenizer.json` |
| `config.json` | `config.json` |

Note the rename: the quantised ONNX file must be saved as `model.onnx`. Restart the engine (or
press **Enable smart search** again) and the toggle becomes available.

Until then the Smart toggle stays visibly unavailable and says why. Search never errors because
of it.

### Local text generation (Ollama)

Settings → Search → **Generate summaries with a local model**. Points at
`http://localhost:11434` by default; there is a **Test** button. Entirely optional, entirely
local. Anything it produces is marked violet — inferred, not measured.

### Video thumbnails

Frames are extracted from videos only if you have `ffmpeg` on PATH and enable it in Settings.
Nothing is bundled or downloaded for this.

---

## 6. Connecting an MCP client

See **[MCP_CLIENT_SETUP.md](MCP_CLIENT_SETUP.md)**. Short version: point a desktop MCP client at

```
venv\Scripts\python.exe  -m app.mcp_stdio     (working directory: backend\)
```

or POST JSON-RPC to `http://127.0.0.1:8127/api/v1/mcp`.

---

## 7. Updating the app

Replace the source, then re-run `install_dependencies.bat` — it reuses the existing `venv`, brings
packages up to date and rebuilds the interface. The database migrates itself on the next start;
your ratings, tags, notes and albums are preserved.

`backend\data\vault.db` is the only file worth backing up.

---

## 8. Uninstalling

Delete the project folder. That is all of it:

* `venv\` — the Python environment,
* `frontend\node_modules\` and `frontend\dist\` — the interface,
* `backend\data\` — database, thumbnails and any embedding model you enabled.

The vault writes nothing to the registry, nothing to `%APPDATA%`, and nothing outside the project
folder — except items you deleted from inside the app, which are in `.vault-trash` inside the
ComfyUI root. Empty that from Settings → Storage first if you want it gone.

Your ComfyUI installation is untouched.

---

## 9. If it does not start

`backend_log.txt` in the project root holds everything the engine printed. Start with
**[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** — it is organised by symptom.
