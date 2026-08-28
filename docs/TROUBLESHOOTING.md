# Troubleshooting

Geekatplay ComfyUI Asset Vault · **Geekatplay Studio — Vladimir Chopine**

Organised by symptom. Two things solve most problems:

* **`backend_log.txt`** in the project root — everything the engine printed, most recent last.
* **The health drawer** — the pulse icon in the top bar, or `GET /api/v1/system/health`.

---

## The engine will not start

`start_app.bat` waits 45 seconds for the port to answer, then prints the last 20 lines of
`backend_log.txt` and stops. Read those lines first; the cause is almost always in them.

| What the log says | What to do |
|---|---|
| `No module named 'app'` | uvicorn was started without `--app-dir backend`. The correct command is `venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8127 --app-dir backend`. There is no `--cwd` option in uvicorn — that was the old, broken invocation. |
| `No module named 'fastapi'` (or pydantic, PIL, …) | The environment is incomplete. Re-run `install_dependencies.bat`; it verifies every package imports before it reports success. |
| `[Errno 10048] … only one usage of each socket address` | Something already holds 8127 — see the next section. |
| `Python virtual environment not found` from the launcher itself | `venv\` is missing. Run `install_dependencies.bat`. |
| `Frontend dependencies not installed` | `frontend\node_modules` is missing. Same fix. |

To watch it start with nothing hidden, run the engine in the foreground:

```bat
venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8127 --app-dir backend
```

A healthy start ends with `Application startup complete.` and a line like
`startup: configured=True path=C:\ComfyUI`. It answers within about a second.

Check it directly:

```bat
curl http://127.0.0.1:8127/api/v1/ping
```

```json
{"pong":true,"t":1787456137060}
```

---

## Port 8127 is already in use

The launcher refuses to start rather than leaving you with two engines fighting over one database.

```bat
stop_app.bat
```

To see what is holding it:

```bat
netstat -ano | findstr :8127
tasklist /FI "PID eq <the pid>"
```

Usually it is a previous engine that outlived its window — the interface exits before the engine
does if the window was closed the hard way. `stop_app.bat` kills whatever is listening on 8127 and
tells you which PID it stopped. If it is genuinely another application, change nothing here: 8127
is the agreed port and the interface's dev-server proxy, the MCP origin allowlist and the test
suite all assume it.

The interface's own port, 3000, is separate. Closing that window does not stop the engine.

---

## The interface loads but says "The vault service is not answering"

The engine is down or on a different port. Confirm with `/api/v1/ping` as above, then check
`backend_log.txt`. If the engine is up and the interface still cannot reach it, you are probably
opening `http://localhost:3000` while the dev server is not running — either start it
(`cd frontend && npm run dev`) or use `http://127.0.0.1:8127/`, which the engine serves itself
once `npm run build` has been run.

---

## The ComfyUI path is wrong, or points at nothing

Symptoms: counts are zero, thumbnails do not render, every rename/move/delete is refused, or the
health drawer reports `comfyui_root` as an error. The error message distinguishes the two cases:
"No ComfyUI folder is configured" means nothing has been set yet, while "The configured folder is
not reachable (drive offline?)" means a folder is set but cannot be found.

Fix it in **Settings → Location**. The field validates live — it tells you whether the folder
exists, whether it looks like a ComfyUI install, and which sub-folders it found, before you can
save. Point it at the folder that contains `models\`, `custom_nodes\` and `output\`. Pointing at
a portable build's parent folder (the drive root beside `python_embeded`) now works too — the
install inside is found automatically.

Every consumer picks the new path up **without a restart**: the indexer, the file-operation root
guard, thumbnails, search and the MCP server. Re-index when offered.

**What happens to the old data:** it is **kept**. The old root is marked retired, not removed. Its
models, workflows and outputs stay in the vault with their ratings, tags, notes and album
membership intact, and the missing-file sweep skips them — so nothing is flagged just because a
drive was unplugged. Retired roots are read-only: rename, move and delete are refused for them.
When you are certain, remove that content explicitly from the Storage tab.

Extra roots from `extra_model_paths.yaml` are re-read from the new install rather than silently
carried over. `extra_model_paths.yaml.hold` is ignored unless you enable it in Settings → Jobs.

You can read the current policy at any time:

```
GET /api/v1/comfyui/path-policy
```

---

## Models show "Unknown" as their base model

This is a statement of confidence, not a failure, and it is deliberately not hidden.

The family is detected from the tensor keys and shapes in the file header. It works reliably for
the families the rules cover. Where nothing matches, the app says `Unknown` — coloured violet —
rather than inventing an answer. Open **Detection signals** in the Details panel and you will see
exactly why, down to `rule:none` with a confidence of `0.00`.

Legitimate reasons a file lands here:

* **A format with no readable keys.** A legacy `.pth` pickle container cannot be read without
  unpickling it, which the app will not do, because unpickling executes code. It is reported as
  `Unsupported Format` and left at `Unknown`.
* **A genuinely unusual architecture** the rules do not cover — upscalers, depth estimators,
  frame interpolators, audio encoders. 108 of the 237 models on the reference install are
  classified as `Other` for exactly this reason.
* **A merge or a fine-tune** whose keys no longer resemble any base family.

`Unknown` never blocks anything: the file is still indexed, searchable, linkable to workflows, and
usable in every list. You can tag it, rate it, note it and file it in an album like any other
model — and once the file is hashed and matched, Civitai often supplies the family the local
header could not.

There is no manual base-model override in this build. If detection is wrong (not merely absent)
for a family the rules do claim to cover, that is a parser bug worth reporting, with the filename
and what **Detection signals** shows.

---

## Civitai shows nothing

**Civitai data only appears after a model has been hashed.** There is no way around this — Civitai
identifies a file by AutoV2, the first 10 hex characters of its full-file SHA-256, so the whole
file has to be read.

Three things must all be true:

1. The model has been hashed — its badge reads `done`, not `unhashed`.
2. **Settings → Search → Allow outbound lookups at all** is on.
3. **Match hashed files against Civitai** is on.

Then start hashing from the **Hash** button in the top bar.

**Budget the time honestly: the full 1.5 TB reference library is about 2.8 hours.** It is
disk-bound. It runs in the background, is cancellable, resumes after a restart, and caches per
`(path, size, mtime)` so nothing is re-read without reason. Hash one category first if you only
care about, say, your LoRAs.

If a model is hashed and matched but Civitai still shows nothing, the file simply is not on
Civitai — a merge, a private model, or something published elsewhere.

Everything else about the model works while unhashed. Only the Civitai section stays empty.

---

## Smart search is unavailable

Search still works — it is lexical, and the toggle says so rather than throwing an error. That is
the intended behaviour, not a bug.

The **Smart** toggle needs a one-time **~23 MB** local model (`all-MiniLM-L6-v2` INT8, 384
dimensions, CPU only through ONNX Runtime — no torch). Turn it on in **Settings → Search →
Enable smart search**.

Check what is missing:

```
GET /api/v1/embeddings/status
```

```json
{"state":"not_installed","reason":"embedding_model_not_installed",
 "install_dir":"…\\backend\\data\\models\\all-MiniLM-L6-v2",
 "index":{"embedded":0,"pending":6182},
 "onnxruntime":{"installed":true,"version":"1.29.0"}}
```

| `reason` | Meaning |
|---|---|
| `embedding_model_not_installed` | The three model files are not on disk. Enable it, or place them manually — below. |
| ONNX Runtime not installed | Re-run `install_dependencies.bat`; `onnxruntime` is in `requirements.txt`. |
| download failed | No route to `huggingface.co`. Use the manual path below. |

### Placing the model manually, offline

Copy three files into

```
backend\data\models\all-MiniLM-L6-v2\
```

| Save as | Source under `https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/` |
|---|---|
| `model.onnx` | `onnx/model_quantized.onnx` |
| `tokenizer.json` | `tokenizer.json` |
| `config.json` | `config.json` |

**The rename matters** — the quantised file must be saved as `model.onnx`. Fetch them on any
machine and copy them across; nothing else is needed.

Then press **Enable smart search** again, or restart the engine. Embeddings are computed
incrementally in the background for all indexed documents and are never recomputed per query.

---

## Thumbnails are missing or blank

Work through it in this order.

1. **The path is wrong.** A bad root means the file cannot be read at all. See the ComfyUI path
   section above, then check `comfyui_root` in the health drawer.
2. **The file is genuinely gone.** Missing files show as missing rather than as broken images —
   see the next section.
3. **The format cannot be decoded.** Pillow handles the common image formats. Anything it cannot
   open gets a generated placeholder tile, which is what you are seeing — it is not an error.
4. **Videos.** Frames are only extracted when `ffmpeg` is on PATH **and** video thumbnails are
   enabled in Settings. Nothing is bundled or downloaded for this. Without it, videos get a
   placeholder.
5. **The cache was trimmed.** The thumbnail cache has a size cap (2048 MB by default) and evicts
   least-recently-used entries. They regenerate on demand; the first scroll after a trim is
   slower.

Test one directly — the `X-Thumb-Source` response header tells you whether it came from the cache
or was just generated:

```bat
curl -I "http://127.0.0.1:8127/api/v1/files/thumbnail?uid=output:1&size=320"
```

```
HTTP/1.1 200 OK
x-thumb-source: cache
content-type: image/webp
```

---

## `FILE_LOCKED` — "ComfyUI is probably holding it"

HTTP **423**. Windows refuses to rename, move or delete a file that another process has open, and
ComfyUI keeps model files open while a model is loaded.

```json
{"error":{"code":"FILE_LOCKED","message":"…","retryable":true}}
```

**Close ComfyUI, then retry.** Unloading the model inside ComfyUI is often enough; closing it is
certain. There is no override and there should not be one — forcing a handle closed under a
running process is how you corrupt a 40 GB checkpoint.

Reading is never affected. Indexing, search, thumbnails and every read-only view work perfectly
well while ComfyUI is running. Only mutations are blocked, and only on the specific locked file.

The same rule protects the ComfyUI updater: it refuses to run while ComfyUI appears to be running.

---

## Long-path errors

Windows limits a path to 260 characters unless long-path support is on. The app applies the
`\\?\` long-path prefix internally, so it can read and write deep paths that Explorer struggles
with.

If you still hit path errors — typically from a deeply nested node package with long example
filenames — enable long paths system-wide:

```powershell
# Administrator PowerShell, then reboot
New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' `
  -Name LongPathsEnabled -Value 1 -PropertyType DWord -Force
```

The scan never aborts on one unreadable path. It records the failure and carries on, and you can
read the list at `GET /api/v1/index/errors`.

Non-ASCII, CJK and emoji filenames are supported throughout, including over the MCP stdio
transport.

---

## Reading `/system/health`

The pulse icon in the top bar, or:

```bat
curl -H "X-Vault-Request: 1" http://127.0.0.1:8127/api/v1/system/health
```

The top-level `status` is the **worst** individual check, so a single unreadable file reads as
`error` overall. Do not react to the headline; read the lines.

| Check | `warn` / `error` means |
|---|---|
| `comfyui_root` | Either no folder is configured yet, or the configured folder is unreachable — the message says which. Fix the path. |
| `scan_roots` | One or more extra scan folders are offline. The mapping and indexed rows are kept — reattach the drive, or remove the folder in Settings → Location. |
| `database` | Journal mode and size. `ok` reads like `WAL, 33.4 MB`. |
| `embeddings` | Smart search is off or its model files are absent. Expected until you enable it. |
| `civitai` | Online enrichment disabled. Expected by default. |
| `ollama` | Local text generation disabled or unreachable. Expected by default. |
| `integrity` | Named files failed the format check — see below. |
| `partial_downloads` | `.crdownload` / `.part` leftovers in a model folder. Harmless; delete them yourself. |
| `suspect_remotes` | A node package whose git remote does not obviously match its folder name. Informational — renamed forks trip it legitimately. |
| `thumb_cache` | Cache size against its cap. |

### `integrity` failures

Two on the reference install, and both are correct:

* `4x-UltraSharp.pth` — `unsupported_format`. A legacy pickle container; the app will not unpickle
  it, because unpickling executes code.
* `flux2-vae-new.safetensors` — `not_a_model`. The header parses but does not describe a model.

`unsupported_format` is a limitation, stated. `truncated` or `unreadable` is worth acting on — that
usually means an interrupted download.

---

## What "missing" means

**A vanished file is flagged, never deleted from the vault.**

When a scan finds an indexed file gone, the prune phase marks it missing and moves it into the
`Missing files` album. Its ratings, tags, notes, album membership, hash and Civitai data all
survive. Reconnect the drive or restore the file and the next scan restores it exactly as it was.

This is why unplugging an external drive does not cost you a year of curation, and why the app
never "cleans up" rows on your behalf. Retired roots — the ones left behind by a path change — are
excluded from the sweep entirely, so their content is not even flagged.

If you genuinely want a missing item gone from the vault, remove it explicitly from the Storage
tab.

---

## A workflow says it will not run

Expected, and useful. **159 of 211** workflows on the reference install are missing at least one
dependency — normal for a library that has accumulated example graphs from 34 node packages.

Select the workflow and read its dependency report. It separates two very different problems.

**Missing node packages.** The graph uses a node class no installed package registers. ComfyUI
cannot load the graph at all. The report names the class, and the ComfyUI-Manager registry entry
where one exists. Install the package with your usual tool — the vault will **not** run a package's
installer or `pip install` for you, because auto-running an untrusted repository's
`requirements.txt` is remote code execution. It tells you the exact command; you decide.

**Missing model files.** The graph names a model you do not have. For each one the report gives
the exact string requested, the **category it belongs in** — resolved from the node input that
referenced it, so `ckpt_name` → `checkpoints`, `lora_name` → `loras`, `unet_name` →
`diffusion_models`, `vae_name` → `vae`, `clip_name` → `text_encoders` — which node asked for it,
and **close matches from your own library** scored by name similarity.

Check those suggestions before you download anything. A score of 1.00 almost always means you
already have the file, just in a different sub-folder — the workflow asked for
`WanVideo/lightx2v_….safetensors` and yours is in `loras\` with no `WanVideo` prefix. That is a
move or a rename, not a 40 GB download. The suggestions are marked violet and labelled
*this is a guess, not a match*, because name similarity is exactly that.

Satisfied dependencies name their match method — `exact_relpath` is firm, `basename` is looser.

**Fetching what is missing.** The **Enable** flow does this, and it always shows you the plan
before it downloads anything: each missing item, the folder it resolves to, the size, and the free
space on each target drive. You then tick what you want; there is no fetch-everything shortcut,
and a plan you did not confirm is refused. If an item is listed as **not fetchable**, no permitted
source publishes it — Civitai and Hugging Face are the only model hosts, and only
registry-declared repositories are cloned. Node packages are cloned but never installed; run their
install step yourself, deliberately.

The reports are available at `GET /api/v1/workflows/{id}/dependencies` and
`GET /api/v1/workflows/{id}/enable/plan`.

---

## The UI stutters briefly during a full reindex

**A known, open issue. It is documented here rather than hidden.**

During a **forced full** reindex, API tail latency rises. Measured on the reference install:

| | |
|---|---|
| `/ping` median | **13.3 ms** — typical requests are unaffected |
| `/ping` p95 | **96–147 ms** against a 50 ms budget |
| `/ping` max | 874 ms |
| Samples over 50 ms | 29 of 235 |

`/ping` does not touch the database, so this is event-loop starvation from thread contention, not
query cost. It has **not** been diagnosed to a specific cause, and the budget has deliberately not
been relaxed to make the number look better.

**Practical impact is small.** It occurs only during a forced full re-parse, which takes about 14
seconds and is rare. Warm incremental scans finish in 0.31–0.37 s and do not show it at all. The
interface stays usable; it stutters.

**What to do:** nothing, unless it bothers you — in which case let the full scan finish before
scrolling a large grid. Prefer the ordinary incremental re-index (`F5`) over a forced full one;
you only need a full re-parse after changing the ComfyUI path or upgrading the app.

---

## Other things worth knowing

**`backend\data\vault.db-wal` can get large.** SQLite's write-ahead log grows during heavy
indexing and is only truncated when the last connection closes. On the reference install it sat at
about 1.1 GB against a 35 MB database. It is not data loss and it is not a leak — it is reclaimed
when the engine stops. Stop the app with `stop_app.bat` if you need the space back. Budget 2–3 GB
for `backend\data\` on a library this size.

**Mutating API calls need a header.** Every `POST` / `PATCH` / `PUT` / `DELETE` must send
`X-Vault-Request: 1`, otherwise you get `400 CSRF_HEADER_MISSING`. Plain `GET`s do not need it.
This is the CSRF defence for a localhost app; the interface sends it automatically.

**The engine is loopback-only.** It refuses to bind anything but `127.0.0.1` unless `ALLOW_LAN=1`
is set deliberately, and then MCP additionally requires a bearer token.

**Every error carries a `request_id`.** It matches a line in `backend_log.txt`:

```json
{"error":{"code":"NOT_FOUND","message":"Model 999999 does not exist.",
          "request_id":"10fab7165d2e…","retryable":false,"docs":"/docs#not_found"}}
```

Branch on `error.code`, never on the message text.

**Scan errors are recorded, not fatal.** `GET /api/v1/index/errors` lists every file the scan
could not read, with the reason. A scan that reports errors still committed everything it could.

**Nothing is deleted without confirmation, and trash is the default.** If something seems to have
vanished, look in the Storage tab's Trash section before assuming the worst. Retention is 30 days.

---

## Reporting a problem

Include:

* the last 40 lines of `backend_log.txt`,
* the output of `GET /api/v1/system/info` and `GET /api/v1/system/health`,
* the `request_id` from the error envelope, if you have one,
* what you clicked.
