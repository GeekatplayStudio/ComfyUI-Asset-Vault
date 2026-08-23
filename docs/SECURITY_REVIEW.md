# Security Review — Geekatplay ComfyUI Asset Vault v2.0

**Reviewer:** `security` agent · **Date:** 2026-08-22 · **Scope:** `backend/app/**`, `frontend/src/**`, `backend/requirements.txt`
**Method:** static review plus **execution**. Every finding below was reproduced by running code, not by reading it.
**Test suite:** `backend/tests/security/` — 343 passed, 1 skipped, 11 xfailed (each xfail is an open finding), `ruff` clean.
**Data safety:** every test is hermetic (synthetic ComfyUI tree in `tmp_path`, throw-away `vault.db`). The owner's library was verified unchanged after the audit: **237 models / 3,834 outputs / 1,866 node classes**, trash empty.

---

## 1. Verdict

| | |
|---|---|
| **Critical** | 0 |
| **High** | 2 — both open, both with a concrete fix and a named owner |
| **Medium** | 5 |
| **Low** | 6 |
| **Informational** | 4 |

**The two Highs are the sign-off blockers.** Neither is a flaw in the C5 grant — both are gaps in controls the plan already required (`BUILD_PLAN §7.1` junctions, `§7.6` CSRF).

What the audit could **not** break, having tried by execution:
* no path traversal escaped a root through any endpoint — 13 hostile folder specs, 8 hostile filenames, absolute paths, `\\?\`, UNC, `%2e%2e`, drive switching, `.. ` / `...` / `.. .` trailing-dot-and-space variants, and >260-character paths were all contained;
* no SQL injection — 12 payloads × 5 list surfaces × sort/group, plus the new `/storage` queries; the schema was intact afterwards;
* no code execution on the parsing path — a `__reduce__` pickle bomb, a `!!python/object/apply` YAML, and a `custom_nodes/__init__.py` that calls `os.system` were all parsed as inert data;
* no CSRF on any of the 43 mutating v1 routes;
* the `civitai_api_key` was never returned, logged, or surfaced through MCP;
* the C8 updater could not be run unconfirmed, could not have arguments injected, and is not reachable from MCP.

---

## 2. Finding table

| ID | Severity | Title | Location | Owner | Status |
|---|---|---|---|---|---|
| **S-01** | **High** | NTFS junctions let the indexing walker read and index files outside every configured root | `app/indexing/walker.py:74,79`, `app/parsers/node_ast.py:164` | `backend-core` | **Open** |
| **S-02** | **High** | `/api/v1/mcp` accepts a browser simple request, so any loopback-origin page can drive the destructive tool surface | `app/mcp/http.py:145`, `app/main.py:126` | `mcp` (+ architect: `MCP_SPEC §9`) | **Open** |
| **S-03** | Medium | No SSE subscriber cap — `ProgressBus._subs` is unbounded | `app/core/progress.py:26,83` | `backend-core` | Open |
| **S-04** | Medium | The updater executable follows `comfyui_path`, which the API accepts, so any directory holding `update\update_comfyui.bat` becomes the confirmed updater | `app/services/comfyui_service.py:240-252`, `app/api/v1/system_router.py:123` | `api-connectivity` + `backend-core` | Open |
| **S-05** | Medium | No `Image.MAX_IMAGE_PIXELS` budget and no `formats=` allowlist — a 324-byte PNG allocates ~480 MB | `app/jobs/thumb_service.py:258-267`, `app/parsers/image_meta.py:143` | `backend-core` | Open |
| **S-06** | Medium | Unlimited MCP session creation defeats the per-session rate limit | `app/mcp/protocol.py:161-173` | `mcp` | Open |
| **S-07** | Medium | No request body size cap — a 64 MB body is fully buffered and parsed before rejection | `app/api/middleware.py`, `app/mcp/http.py:145` | `api-connectivity` | Open |
| **S-08** | Medium | SSRF: `/system/ollama/test` and the `ollama_url` config key accept any absolute URL | `app/api/v1/system_router.py:460`, `app/services/ollama_service.py:28` | `api-connectivity` + `backend-core` | Open |
| **S-09** | Low | Dependency floors admit vulnerable builds (`pillow>=10.3`, `python-multipart>=0.0.6`, no explicit `starlette` pin) | `backend/requirements.txt` | `backend-core` | Open |
| **S-10** | Low | `POST /system/roots` accepts any existing directory, including `C:\Windows`, widening the root guard with no warning | `app/api/v1/system_router.py:403` | `api-connectivity` | Open |
| **S-11** | Low | `shutil.rmtree` in the permanent-delete path is gated on `os.path.isdir`, not on `kind == "node_package"` | `app/services/file_ops.py:412-414` | `backend-core` | Open |
| **S-12** | Low | `trash_empty` calls `rmtree(force=True)` on a DB-supplied path without re-validating it is inside a root's `.vault-trash` | `app/services/file_ops.py:915-940` | `backend-core` | Open |
| **S-13** | Low | `torch_zip` caps read size but not archive entry count | `app/parsers/torch_zip.py:70-83` | `backend-core` | Open |
| **S-14** | Low | `/system/validate-path` caps its models/output/input walk but not its `walk_json` workflow count | `app/api/v1/system_router.py:170-175` | `api-connectivity` | Open |
| **S-15** | Info | `keep_extension` rewrites `new_name` before validation, so a rejected-looking name silently becomes a different one | `app/services/file_ops.py:290-294` | `backend-core` | Accepted |
| **S-16** | Info | The safetensors header cap is 200 MB; `BUILD_PLAN §7.12` says 100 MB | `app/parsers/safetensors_header.py:19` | `backend-core` | Accepted |
| **S-17** | Info | The app's own `data/` directory is a configured root, so file operations can target it | `app/core/config_service.py` | `backend-core` | Accepted |
| **S-18** | Info | `embed_service` downloads over `follow_redirects=True` with no host allowlist, no expected hash, and no free-space check | `app/jobs/embed_service.py:144-184` | `backend-core` | Accepted (precedent for C9 — see §5) |

---

## 3. High findings in detail

### S-01 — NTFS junctions escape every configured root (High)

**Location** `backend/app/indexing/walker.py:74,79`; `backend/app/parsers/node_ast.py:164`

**What is wrong.** Both walkers exclude reparse points with `entry.is_dir(follow_symlinks=False)`. On Windows that test excludes *symlinks* only: `DirEntry.is_symlink()` is true for `IO_REPARSE_TAG_SYMLINK` but **false for `IO_REPARSE_TAG_MOUNT_POINT`** — a directory junction. `is_dir(follow_symlinks=False)` therefore returns `True` for a junction and the walker descends straight through it. Creating a junction on Windows needs no elevation (`mklink /J`), unlike a symlink.

**Reproduction** (executed; `backend/tests/security/test_traversal.py::test_indexing_walker_does_not_descend_a_junction`):

```
mklink /J <root>\models\checkpoints\linked  <somewhere-outside-every-root>
mklink /J <root>\custom_nodes\pkg\linked    <somewhere-outside-every-root>
POST /api/v1/index/start
GET  /api/v1/models      -> a row whose abs_path is  <root>\models\checkpoints\linked\private.safetensors
GET  /api/v1/node-classes -> class "LeakedFromOutsideRoot", parsed from a .py outside the root
```

**Impact.** The indexer opens, parses and records files that live outside every configured root: safetensors headers, PNG/WebP metadata (including generation prompts), and the Python source of arbitrary third-party packages. Absolute paths and parsed metadata land in `vault.db` and are then served by `/models`, `/node-classes`, `/search`, `/storage/candidates` and the MCP read tools. A junction pointed at `C:\Users` turns a "ComfyUI scan" into a full profile enumeration and an unbounded walk.

**What still holds** (verified): the *write* path is safe. `file_ops` resolves through `os.path.realpath`, which follows the junction, so `/files/raw`, `/files/download`, rename, move and delete all return `403 PATH_NOT_ALLOWED` on a junctioned row and the file outside the root survives. This is confinement by a second, independent check — not by the walker.

**Why it matters here.** The threat is not hypothetical: the vault's own threat model is third-party `custom_nodes` packages, and C9 will have the app help install them from git repos. A repo that ships a junction gets the vault to read whatever it points at.

**Recommendation** (owner: `backend-core`). In both walkers, treat any reparse point as a non-directory. `os.scandir` exposes it without an extra syscall:

```python
st = entry.stat(follow_symlinks=False)
if st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
    continue                      # junction, symlink, or mount point
```
or equivalently `entry.stat(follow_symlinks=False).st_reparse_tag != 0` on Python 3.12. Record a `scan_errors` row (`code='REPARSE_POINT_SKIPPED'`) so the skip is visible rather than silent, and add the same guard to `walker.top_level_dirs`. Flip `test_indexing_walker_does_not_descend_a_junction` and `test_node_scanner_does_not_descend_a_junction` from xfail to pass.

---

### S-02 — The MCP endpoint has no CSRF control (High)

**Location** `backend/app/mcp/http.py:145` (`mcp_post`); `backend/app/main.py:126` (`require_vault_request` is a dependency of the `v1` router only, and `mcp_http.router` is included on `app` directly)

**What is wrong.** Every mutating REST route requires `X-Vault-Request: 1`, which a cross-origin *simple* request cannot set. `/api/v1/mcp` is outside that dependency and relies solely on `Origin` validation — and `ORIGIN_OK` accepts **any loopback origin on any port**:

```python
ORIGIN_OK = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\]|::1)(:\d{1,5})?/?$", re.I)
```

`mcp_post` then calls `json.loads(await request.body())` **without checking `Content-Type`**, so a `text/plain` POST — a CORS *simple* request that triggers no preflight — reaches the dispatcher.

**Reproduction** (executed; `backend/tests/security/test_csrf.py::test_mcp_cannot_be_driven_by_a_browser_simple_request`). A page served at `http://127.0.0.1:8188` — ComfyUI's own origin, which loads third-party custom-node JavaScript — runs:

```js
await fetch("http://127.0.0.1:8127/api/v1/mcp", {
  method: "POST", headers: {"Content-Type": "text/plain"},
  body: JSON.stringify({jsonrpc:"2.0", id:1, method:"initialize",
    params:{protocolVersion:"2025-06-18", capabilities:{}, clientInfo:{}}})});
// -> 200, Mcp-Session-Id returned; then, with that session id:
//    {"method":"tools/call","params":{"name":"vault_delete","arguments":{"uids":[...]}}}
```

Observed: `200`, session issued, `vault_delete` executed and an `mcp_audit` row written. No `X-Vault-Request` header, no preflight, no user interaction.

**Impact.** Any page a browser can be made to load from a loopback origin gets the **full C5 destructive surface**: rename, move, delete (200 items per call, 120 calls/minute per session, and S-06 removes even that bound), `vault_trash_empty` with `confirm:true`, `vault_create_folder`. Against a 1.5 TB irreplaceable library. The most plausible carrier is ComfyUI itself at `127.0.0.1:8188`, whose UI executes JavaScript shipped by third-party node packages — the same untrusted code this app is careful never to import.

The rails still bound the blast radius correctly — trash-backed by default, `confirm:true` for permanent, everything audited — so this is data *disruption* (and, via `vault_trash_empty`, destruction), not silent loss. That is why it is High and not Critical.

**Recommendation** (owner: `mcp`; needs an architect amendment to `MCP_SPEC §9`). Apply all three, cheapest first:

1. **Require `Content-Type: application/json`** on `POST /api/v1/mcp`; reject anything else with `415`. This alone closes the simple-request path, because a JSON content type forces a preflight the server does not answer.
2. **Require `X-Vault-Request: 1`** on `/api/v1/mcp` as well. MCP clients are not browsers; setting a header is free for them and impossible for a simple request. Document it in `MCP_CLIENT_SETUP.md`.
3. **Narrow `ORIGIN_OK`** to the vault's own origin and the two dev ports, rather than every loopback port.

Then flip `test_mcp_cannot_be_driven_by_a_browser_simple_request` from xfail to pass.

`MCP_SPEC §9` currently names Origin validation as the anti-rebinding control and says nothing about CSRF. That statement is incomplete and should be amended by the architect: **Origin validation stops DNS rebinding; it does not stop a same-machine loopback page.**

---

## 4. Medium and Low findings

### S-03 — No SSE subscriber cap (Medium, `backend-core`)
`ProgressBus._subs` is a plain list with no maximum; `/index/stream`, `/hash/stream`, `/embeddings/stream` and `/comfyui/update/stream` all append to it. Each subscriber holds an `asyncio.Queue(maxsize=1008)`, and every published event fans out across the whole list. `BUILD_PLAN §7.12` requires an SSE subscriber cap; there is none (`grep MAX progress.py` yields only `MAX_QUEUE`). **Fix:** add `MAX_SUBSCRIBERS` (32 is generous for a single-user desktop app) and refuse a new subscription with `503 FEATURE_UNAVAILABLE` past it. The per-subscriber `overflow` drop is already correct and should stay.

### S-04 — The updater path follows caller-settable config (Medium, `api-connectivity` + `backend-core`)
`discover_updaters` builds candidates as `<comfyui_path>.parent / "update" / update_comfyui.bat`. `PATCH /system/config` accepts any directory that merely contains `models/` and `main.py`. Proven by execution (`test_the_updater_must_live_under_the_verified_comfyui_install`): pointing config at a staged directory made `/comfyui/update/plan` resolve and offer that directory's `.bat` as the confirmed updater. The `confirm_path` equality check is intact — it just confirms *the attacker's* path.

This needs the CSRF header, so it is not remotely reachable; it is a privilege-expansion step for anything that already has same-origin or dev-origin access (see S-02, and CORS in dev). **Fix:** (a) require a stronger install proof before a path is accepted as a ComfyUI root (`comfyui_version.py` **and** `main.py` **and** `models/`, not `models/` plus either); (b) make a `comfyui_path` change invalidate any in-flight update plan, and have `/update/plan` return a short-lived opaque `plan_token` that `/update/run` must echo alongside `confirm_path`; (c) surface a "the updater path changed since you confirmed" warning in the UI. `test_changing_the_install_path_changes_what_would_run` already asserts that a stale confirmation is rejected — keep it.

### S-05 — Image decompression bombs (Medium, `backend-core`)
`Image.MAX_IMAGE_PIXELS` is never set, so Pillow's default applies: a `DecompressionBombWarning` at 89 Mpx and a hard error only at 178 Mpx. Executed: a **324-byte** PNG declaring 20000×8000 loaded successfully and allocated ~480 MB. `thumb_service._generate` calls `im.load()` (a full decode), sets `ImageFile.LOAD_TRUNCATED_IMAGES = True` globally, and passes no `formats=` allowlist to `Image.open` — so the PSD, FITS, JPEG2000 and raw-codec paths are live on files the vault did not write. `image_meta.read_image` opens from a path string with the same exposure.

The endpoint degrades correctly (a bomb over the hard limit yields a placeholder, not a 5xx — asserted), so this is memory pressure, not a crash. **Fix:** set `Image.MAX_IMAGE_PIXELS = 64_000_000` explicitly in `thumb_service` and `image_meta`, pass `formats=["PNG","JPEG","WEBP","GIF","BMP","TIFF"]` to both `Image.open` sites, and record an integrity note rather than a silent placeholder when the budget is exceeded.

### S-06 — Unlimited MCP sessions (Medium, `mcp`)
`SessionStore.create` sweeps expired sessions but has no maximum. Executed: 300 `initialize` calls produced 300 live sessions. The 120-call/minute rate limit is per-session, so rotating sessions removes it entirely, and each session is retained memory. **Fix:** cap the store (64 is ample), evict least-recently-used past the cap, and add a per-transport call budget alongside the per-session one.

### S-07 — No request body cap (Medium, `api-connectivity`)
Neither uvicorn nor the app imposes a `Content-Length` limit. A 64 MB JSON body to `/api/v1/fileops/rename` was fully read and parsed before Pydantic's `max_length=255` rejected the field; `/api/v1/mcp` does `await request.body()` with no bound at all. `BUILD_PLAN §7.12` requires JSON size caps. **Fix:** an ASGI middleware that rejects `Content-Length` above 2 MB (and streams-with-no-length above the same budget) with `413 PAYLOAD_TOO_LARGE`, exempting nothing — no v1 endpoint has a legitimate multi-megabyte body.

### S-08 — SSRF through the Ollama URL (Medium, `api-connectivity` + `backend-core`)
Executed: with `ollama_enabled` true (settable in the same session), `POST /system/ollama/test {"url": "http://127.0.0.1:<port>"}` produced a real outbound GET to the caller-chosen host and port, and the response body reported `available`, the HTTP status and the latency — a working host/port scanner. Worse, `PATCH /system/config {"ollama_url": ...}` **persists** an arbitrary URL, and `POST /ai/describe` sends the asset fact sheet — including a workflow's `positive_prompt` — to it. That is a data-exfiltration sink, not just a probe. **Fix:** validate both `OllamaTestRequest.url` and the `ollama_url` config key against `http(s)://(localhost|127.0.0.1|[::1])(:port)?` — Ollama is a local service by definition — and reject anything else with `422`. If a LAN Ollama must be supported, gate it behind the same explicit `ALLOW_LAN` opt-in the bind uses.

### S-09 — Dependency floors (Low, `backend-core`)
Every **installed** version is clean. The **floors** are not, and a fresh `pip install -r requirements.txt` on a resolver that honours them lands on vulnerable builds.

| Package | Floor | Advisories affecting the floor | Fixed in | Reachable here? |
|---|---|---|---|---|
| `pillow` | `>=10.3` | 17 CVEs (CVE-2026-25990, -40192, -42308/-42310/-42311, -54058/-54059/-54060, -55379/-55380/-55798, -59197…-59205) | **12.3.0** | **Yes** — `Image.open` + `im.load()` on files the vault did not write; compounds S-05 |
| `python-multipart` | `>=0.0.6` | 9 CVEs incl. CVE-2024-24762 (ReDoS), CVE-2026-24486 (path traversal) | 0.0.31 | **No** — no `UploadFile`/`File(`/`Form(`/`request.form()` anywhere in `app/` |
| `starlette` | unpinned (`fastapi>=0.110` caps it at 0.36.3) | 8 CVEs; notably **CVE-2026-48818** (UNC/NTLM leak via `StaticFiles` on Windows) and **CVE-2025-62727** (O(n²) Range DoS in `FileResponse`) | **1.3.1** | **Yes** — `StaticFiles` at `main.py:167`, `FileResponse` at `main.py:174,189` and `files_router.py:167` |
| `pydantic` | `>=2.0.0` | CVE-2024-3772 (ReDoS) | 2.4.0 | No — no `EmailStr` |
| `PyYAML` | `>=6.0.1` | none | — | n/a — and every known PyYAML CVE is a `yaml.load`/`FullLoader` issue, unreachable through `safe_load`, which is the only call site (`extra_paths_yaml.py:30`) |
| `onnxruntime` | `>=1.17` | **none found** | — | The heap-overflow CVEs that surface in a search are filed against the separate `onnx` package, which this project does not depend on |
| `tokenizers` | `>=0.15` | none found (no GHSA, no RUSTSEC entry) | — | n/a |
| `uvicorn`, `httpx`, `numpy` | as pinned | none found | — | n/a |

**Fix:**
```
pillow>=12.3.0        # was >=10.3  — 17 CVEs, reachable through Image.open
starlette>=1.3.1      # NEW explicit pin — CVE-2026-48818 (UNC/NTLM) is Windows-relevant
fastapi>=0.135        # so the starlette pin is satisfiable
pydantic>=2.4.0       # free to fix
# python-multipart    # DELETE — unused; only generates audit noise
```
`PyYAML`, `onnxruntime`, `tokenizers`, `uvicorn`, `httpx` and `numpy` need no change. **All three D9 dependencies are justified:** `PyYAML` for `extra_model_paths.yaml` (C2/D4, `safe_load` only), `onnxruntime` + `tokenizers` for the CPU-only local embedder that exists specifically to avoid a torch dependency (C2) — and keeping torch out of the tree is itself the single largest reduction in this app's attack surface.

### S-10 — `POST /system/roots` accepts any directory (Low, `api-connectivity`)
Executed: `{"path": "C:\\Windows", "kind": "extra_workflows"}` returned `201`. That directory becomes a scan root and therefore a `resolve_within_roots` root, so anything indexed under it becomes a legitimate file-operation target. It is a deliberate user action behind the CSRF header and no MCP tool exposes it, hence Low — but "roots enforced" is only as strong as what may become a root. **Fix:** refuse system directories (`%WINDIR%`, `%PROGRAMFILES%`, `%SYSTEMROOT%`, a bare drive root), require the directory to contain at least one `.json` workflow, and return a `warnings[]` array the UI must display before the root is saved.

### S-11 — `rmtree` is gated on `isdir`, not on kind (Low, `backend-core`)
`file_ops.delete` chooses `shutil.rmtree` from `is_dir = os.path.isdir(long_path(old))`. The only uid kind that should ever resolve to a directory is `node_package` (`DIRECTORY_KINDS` exists and says so), but the delete path does not consult it. A `models`/`outputs` row whose `abs_path` ever pointed at a directory would be recursively deleted. **Fix:** `if is_dir and info["kind"] not in DIRECTORY_KINDS: raise ValidationError(...)` before the `rmtree`, so the recursive branch is reachable only through the one deliberate case.

The deliberate case itself was checked hard and is sound: it is trash-backed by default (a same-volume `shutil.move`, O(1)), captures the `node_classes` rows for an exact restore, warns on a dirty git checkout, and refuses rename/move on a package with a specific displayable reason.

### S-12 — `trash_empty` rmtree is not re-validated (Low, `backend-core`)
`_purge_slot(slot, force=True)` calls `shutil.rmtree(ignore_errors=True)` on `dirname(trash_items.trash_path)` straight from the database, with no containment check. The value is only ever written by `_to_trash` under `<root>/.vault-trash/`, so it is not attacker-controlled today — but it is the one `rmtree` in the codebase with no guard in front of it, and it runs on startup via `purge_expired()`. **Fix:** assert `is_contained(slot, root.path)` **and** that the path's parent basename is `TRASH_DIRNAME` before the `rmtree`, and skip with a logged warning otherwise.

### S-13 — `torch_zip` has no entry-count cap (Low, `backend-core`)
Read size is capped correctly (`MAX_PICKLE = 32 MB`, checked against the *declared* uncompressed size, so a zip bomb is refused in 7 ms — verified). `zf.namelist()` is unbounded: a 20,000-entry archive parsed in 152 ms, but the whole central directory is materialised in memory. `BUILD_PLAN §7.13` asks for an entry cap. **Fix:** refuse above 100,000 entries with `integrity='unsupported_format'`. Zip-slip is already a non-issue — nothing is ever extracted to disk (verified).

### S-14 — `/system/validate-path` workflow walk is uncapped (Low, `api-connectivity`)
`_bounded_preview` enforces 60,000 entries and a 4-second deadline on the models/output/input walk, then falls through to `sum(1 for _ in walker.walk_json(directory))` for two workflow directories with **no cap and no deadline**. **Fix:** move the workflow count inside the same budget.

---

## 5. C9 — security requirements for the not-yet-built downloader

C9 will fetch multi-GB files from the internet and place them inside the model roots. These are the requirements the implementing agent must meet, written so each can be tested. `backend/tests/security/` will be extended with one test per item when C9 lands.

**R1 — Host allowlist, no arbitrary URL fetch.**
A frozen module-level constant lists the permitted download hosts: `civitai.com`, `*.civitai.com`, `huggingface.co`, `*.hf.co`, `cdn-lfs*.huggingface.co`, plus registry-declared git remotes on `github.com`/`gitlab.com`. Every URL is parsed and its host matched against that list **after** normalisation, with `https` mandatory. *Test:* a resolve report containing `http://evil.test/model.safetensors`, `https://civitai.com.evil.test/x`, `https://huggingface.co@evil.test/x` and `file:///C:/Windows/win.ini` yields four refusals and zero requests. No endpoint or MCP tool accepts a caller-supplied URL — the URL always comes from the Civitai/HF API response or the ComfyUI-Manager registry, never from the request body.

**R2 — Redirects are re-validated, never followed blindly.**
`follow_redirects=False`; each hop is re-checked against R1 before it is taken, with a maximum of 5 hops. No `Authorization` header survives a host change. *Test:* an allowlisted host that 302s to `http://evil.test` aborts with `UPSTREAM_UNAVAILABLE` and never issues the second request. (This is the concrete lesson of S-18: `embed_service._download_model` uses `follow_redirects=True` today.)

**R3 — Destination is derived, never supplied.**
The target directory comes from the node input name via a frozen map (`ckpt_name`→`checkpoints`, `lora_name`→`loras`, `unet_name`→`diffusion_models`, `vae_name`→`vae`, `clip_name`→`text_encoders`, …). The filename is passed through `pathsafe.validate_filename()` and the final path through `resolve_within_roots()` **before the first byte is written**. A server-supplied `Content-Disposition` filename is treated as a hint only and is subject to the same validation. *Test:* a source advertising `../../../../Windows/System32/evil.dll`, `C:\evil.bin`, `CON`, `x.safetensors:ads` and a 300-character name is refused in all five cases with nothing written; a `.part` file never appears outside the resolved root.

**R4 — Verify before placing; quarantine on mismatch.**
Download to `<target>.part`, hashing as bytes arrive. Compare against the size and, where the source publishes one, the SHA-256/AutoV2 from the API response. Only on a match does `os.replace` move it into place. On a mismatch the `.part` is moved to `<root>/.vault-quarantine/` with a JSON reason file, a `scan_errors` row is written, and the API reports `INTEGRITY_MISMATCH` — never a silent overwrite, never a placement. *Test:* a source advertising hash `A` serving content hashing to `B` leaves the target directory unchanged and one quarantine entry present.

**R5 — Never overwrite an existing file implicitly.**
If the destination exists, the default is to refuse; `on_conflict` accepts `skip`/`keep_both` only. `overwrite` is not offered by the download path at all. *Test:* a download whose target already exists returns `409 CONFLICT` and leaves the existing bytes byte-identical.

**R6 — Free space is checked before and during.**
`shutil.disk_usage` on the target root before starting; refuse with `INSUFFICIENT_SPACE` when the advertised size plus a 5% margin exceeds free space. Re-check every 256 MB and abort cleanly if space falls below the remaining need. The owner's drive is 86% full (272 GB free of 1.9 TB), so this is a routine condition, not an edge case. *Test:* a monkeypatched `disk_usage` reporting 1 GB free refuses a 2 GB download before opening a socket.

**R7 — Node packages: clone or report only. Never execute anything.**
No `pip install`, no `python setup.py`, no `install.py`, no `requirements.txt` processing, no post-clone hook — automatically or otherwise. The UI displays the exact command for the user to run themselves. A cloned repo is never imported, and the existing AST-only rule (`§7.3`) continues to apply to everything under `custom_nodes/`. *Test:* a static assertion that no `subprocess`/`exec`/`eval`/`importlib` call site exists in the C9 module, and a fixture repo containing a hostile `install.py` and `requirements.txt` is cloned with neither file touched — the same shape as the existing `test_a_malicious_init_py_is_parsed_never_run`.

**R8 — Git clones are constrained.**
`--depth 1`, no submodules (`--recurse-submodules` is never passed), `GIT_TERMINAL_PROMPT=0`, `core.hooksPath=/dev/null` (or `--config core.hooksPath=`), remote validated by R1, list argv with `shell=False`, and a wall-clock timeout. A `.gitmodules` in the fetched repo is reported, never acted on. *Test:* a repo declaring a submodule pointing at `file:///C:/` clones without fetching it.

**R9 — Explicit per-item consent, and the plan is what runs.**
The dependency report is produced first and shows, per item: source URL, host, size, destination path, and whether a hash is available. Nothing downloads until the user selects items. The execute call echoes a short-lived opaque `plan_token` bound to the exact item set, the same shape the C8 updater's `confirm_path` uses — so a plan that has gone stale cannot execute. *Test:* an execute call with a modified item set, or a token from a superseded plan, is refused with `422` and issues no request.

**R10 — From MCP: same rules, plus audit.**
If a C9 tool is exposed to MCP at all, it inherits every rule above, is `mutating=True` and `audited=True`, is refused in `mcp_read_only` mode, is capped at 200 items, and takes **uid/workflow-id input only — never a URL and never a path**. The confirmation token requirement is not waived for agents. *Test:* the existing `test_no_tool_accepts_a_filesystem_path_or_a_url` and `test_read_only_mode_refuses_every_mutating_tool` extend to the new tool with no modification.

**R11 — Cancellable, resumable, and bounded.**
Reuses the existing job/SSE infrastructure. A cancelled or crashed download leaves only a `.part` file, never a partial file at the real name. Resume uses HTTP `Range` and re-verifies the full hash at completion, never trusting the already-downloaded prefix. *Test:* killing a download mid-stream leaves no file at the target name; resuming and completing produces a file whose hash matches.


### 5.1 C9 as built — where each requirement is asserted

C9 landed on 2026-08-22. `backend/tests/security/test_enable_downloader.py` holds **84 tests**
against a synthetic ComfyUI tree in `tmp_path` and a local fixture HTTP server that records every
request it receives — which is what lets the negative cases assert *"the second request was never
issued"* rather than merely *"the download failed"*. Nothing in the file touches the owner's real
install and nothing in it reaches the internet.

| # | Asserted by |
|---|---|
| R1 | `test_r1_*` — the four documented refusals raise and the fixture server's log stays empty; the frozen list is compared literally; bare IPs, non-`https` schemes, credentials in the authority and IDNA lookalikes are refused; no `/enable` route parameter or body field and no `enable_*` tool property is named `url`/`path` |
| R2 | `test_r2_*` — `follow_redirects=False` asserted statically; a hop off the list raises `UPSTREAM_UNAVAILABLE`; a 302 to a bare-IP origin **on the fixture server itself** never appears in its own request log; `Authorization` is dropped on a host change; the 5-hop budget terminates a loop |
| R3 | `test_r3_*` — all five hostile filenames refused with nothing written and no `.part` anywhere under `tmp_path`; the class/input → folder table checked for eight loaders; the resolved path proven inside a configured root; a `Content-Disposition` filename validated identically and discarded when it fails |
| R4 | `test_r4_*` + `test_the_whole_path_quarantines_a_bad_file_and_records_a_scan_error` — advertised hash A serving content hashing to B leaves the target folder byte-identical, produces one quarantine slot with a `reason.json` naming both digests, writes a `scan_errors` row with `code='INTEGRITY_MISMATCH'`, and marks the batch's `scan_jobs` row failed |
| R5 | `test_r5_*` — an existing destination raises `CONFLICT` before a socket is opened; `overwrite` is absent from `ON_CONFLICT` and rejected by the API schema; `skip` and `keep_both` leave the original bytes untouched |
| R6 | `test_r6_*` + `test_the_whole_path_refuses_a_batch_that_will_not_fit` — a monkeypatched `disk_usage` reporting 1 GB free refuses a 2 GB download with `INSUFFICIENT_SPACE` before any request and before any `enable_jobs` row exists; the 5 % margin refuses a download that would only just fit; the 256 MB re-check is asserted present |
| R7 | `test_r7_*` — an AST sweep proves no `eval`/`exec`/`compile`/`__import__`/`pickle`/`importlib`/`os.system` call site anywhere in `app/enable`, and no `subprocess` import outside `git_fetch.py`; exactly one `subprocess` call site exists, with a timeout, no `shell`, and an argv taken from `build_argv`; no `run`-family call anywhere in the package names `pip`, `install.py`, `requirements.txt` or `setup.py`; a fixture repo carrying a hostile `install.py` and `requirements.txt` is inspected with both files byte-identical afterwards and the marker file never created |
| R8 | `test_r8_*` — the argv carries `--depth 1`, `--single-branch`, `--no-tags`, `--no-recurse-submodules`, `core.hooksPath=<empty dir>`, `protocol.file.allow=never`, `protocol.ext.allow=never` and never `--recurse-submodules`; the environment sets `GIT_TERMINAL_PROMPT=0`; an existing target is never clobbered; and a **real** local repo declaring a submodule at `file:///C:/` is cloned with the module's own flag tuple and the submodule is not fetched |
| R9 | `test_r9_*` — an item outside the plan, a superseded plan, an expired plan, a token for another workflow and an empty selection are each refused with a distinct reason; over REST all three refusal shapes return `422` with the fixture server's log still empty |
| R10 | `test_r10_*` — the tool is `mutating`, `audited`, capped at 200 `item_ids`, closed-schema, uid-only, has no `overwrite`, refuses without `confirm=true`, is refused in `mcp_read_only` mode, and writes an `mcp_audit` row naming the workflow uid |
| R11 | `test_r11_*` + `test_a_cancelled_batch_stops_and_keeps_only_the_part_file` — a cancelled transfer leaves exactly one `.part` and no file at the real name; a resume completes and matches the full-file hash; a **poisoned** resume prefix of the right length is caught by the full re-hash and quarantined; a server that ignores `Range` restarts cleanly; a `running` row from a dead process returns to `queued` |

**One deliberate reading, recorded so nobody has to re-derive it.** R7 asks for "a static assertion
that no `subprocess`… call site exists in the C9 module" while R8 specifies exactly how `git clone`
must be invoked. Both are satisfied by splitting the package: every module in `app/enable` is free
of execution primitives, and `git_fetch.py` alone may import `subprocess`, may start only `git`, and
never runs anything that arrived in the clone. `tests/security/test_no_code_execution.py`'s
whole-app allowlist was widened from two call sites to three by an explicit, named edit.

**Two clarifications on where a URL may come from.** R1 says the URL always comes from the
Civitai/HF API response or the ComfyUI-Manager registry. As built there are three local origins, and
every one of them is validated by `hosts.check` before use:

1. the workflow file's own `models[]` manifest (top level, `extra.models`, or a node's
   `properties.models`) — this is where an official template records the weights it needs;
2. a `download_url` the vault already cached on an indexed model row, which Civitai enrichment wrote
   keyed on a SHA-256 the vault computed itself;
3. the ComfyUI-Manager registry, for a node package's git remote.

There is deliberately **no filename lookup against any API**: ARCHITECTURE §8.4 states that outbound
requests carry only hashes and repo names, and asking Civitai "who has a file called X?" would leak
exactly what that rule protects. A missing model with no declared source is reported with its
resolved destination and the reason, never guessed at. `sources.py` is asserted free of `httpx`.

**Two error codes were added to the API_CONTRACT §0.2 registry**: `INSUFFICIENT_SPACE` (507) and
`INTEGRITY_MISMATCH` (502).

---

## 6. Checklist coverage (BUILD_PLAN §7)

| # | Item | Result |
|---|---|---|
| 1 | Path traversal (`..`, `%2e%2e`, UNC, `\\?\`, ADS, 8.3, junctions/symlinks, drive switch, case, trailing dots/spaces, >260 chars) | **Pass except junctions — S-01.** All other shapes contained; 58 unit + 39 endpoint assertions |
| 2 | No client-supplied paths on any endpoint | **Pass.** Only `/system/config`, `/system/validate-path`, `/system/wizard/complete`, `/system/roots` (S-10) and the updater's echo-only `confirm_path`. The 15 new `/storage/` and `/comfyui/` endpoints take none. `/api/outputs/file?path=` is gone and asserted gone |
| 3 | No code execution on the parsing path | **Pass.** No `import`/`exec`/`eval`/`compile`/`pickle`/`torch`/`subprocess` under `parsers`, `indexing`, `search`. `torch_zip` imports only `zipfile` + `pickletools`. Proven by execution against a live `__reduce__` bomb and a hostile `__init__.py` |
| 4 | YAML `safe_load` only | **Pass.** One call site; `!!python/object/apply` refused by execution; anchor bomb harmless |
| 5 | SQL injection | **Pass.** 127 assertions. `sort`/`group`/`reason` map through frozen dicts; the v0 `sort_column` shape is statically asserted absent, including in the new `reclaim_score` queries |
| 6 | CSRF | **Pass for REST — fails for MCP (S-02).** All 43 mutating v1 routes enforce `X-Vault-Request`, walked from the live OpenAPI document |
| 7 | Bind posture, `ALLOW_LAN`, CORS | **Pass.** Loopback default; `SystemExit` on a LAN bind without `ALLOW_LAN=1`, logged loudly when set; MCP HTTP refuses to mount on the LAN without a token; CORS never `*`, never credentialed, loopback dev ports only |
| 8 | MCP posture | **Pass except S-02 and S-06.** Origin validated, sessions terminable, 24 tools, no path/URL input, closed schemas, no file-content tool, no SSRF pivot, rate limited |
| 9 | Secrets | **Pass.** `civitai_api_key` never returned, never logged, never reachable through MCP; only `civitai_service` reads it and only `https://civitai.com/api/v1` receives it |
| 10 | Log hygiene | **Pass.** No traceback in any response body; `request_id` matches the header; no prompt or key reaches the log; MCP read tools log argument *keys* only, while mutations log values by C5 design |
| 11 | Delete safety | **Pass.** Permanent requires `confirm:true` on REST, storage cleanup and MCP; trash is the default and was proven reversible end-to-end; the one recursive delete is the deliberate node-package case (hardening: S-11, S-12) |
| 12 | DoS caps | **Partial.** Header, GGUF, graph, AST, file-count, `limit` and batch caps all enforced. Missing: SSE subscriber cap (S-03), body size cap (S-07), image pixel budget (S-05) |
| 13 | Zip/archive | **Pass with a gap.** No extraction, so no zip-slip; read size capped; entry count uncapped (S-13) |
| 14 | Dependency review | **Pass with floor bumps required (S-09).** All three D9 dependencies justified; installed versions clean |

---

## 7. C5 verdict

**The C5 grant is implemented as decided, and all six required rails hold.** This audit does not recommend narrowing it.

| Rail | Verdict |
|---|---|
| 1. Trash-backed default | **Holds.** `mode` defaults to `"trash"` in the schema; a real delete-then-restore round trip was executed and the file returned to its original path |
| 2. `confirm:true` for permanent | **Holds.** Refused for absent, `false`, and via `vault_trash_empty` with `confirm` a *required* schema field. Nothing was deleted in any refusal |
| 3. `mcp_audit` with argument values | **Holds.** Every mutating call writes a row with tool, transport, session, outcome, elapsed time and the full argument JSON. Read tools write nothing, and their argument *values* stay out of the log |
| 4. 200-item batch cap | **Holds.** Declared as `maxItems` in every list schema and enforced before any work: 201 uids is refused with an instruction to page |
| 5. Roots enforced, uid-only | **Holds at the tool boundary.** No tool takes a path or a URL, every schema is `additionalProperties:false`, an injected `path` argument is a hard `-32602`, and every mutation goes through the same `file_ops`/`pathsafe` the UI uses. **Caveat: S-01** — the *indexer* can put rows outside a root into the DB, which those tools then read (they cannot act on them) |
| 6. `mcp_read_only` switch | **Holds.** All 9 mutating tools refuse with "Nothing was changed" when set; read tools keep working; the stdio `--read-only` flag forces it independently of config |

The one caveat is not a rail failure — it is S-02, which lets **the wrong caller** reach the correctly-railed surface.

## 8. Updater verdict (C8.3)

**The updater is correctly built. It cannot be run without confirmation, arguments cannot be injected, and it is not reachable from MCP.** Verified by execution:

* `/update/plan` returns the resolved absolute path, the exact argv, and the working directory — the confirmation dialog can promise precisely what runs;
* `run` without `confirm_path` → `422`; with `cmd.exe`, `powershell.exe`, a bare filename, or a relative path → `422`; with the correct path plus `" & calc.exe"`, `" && cmd"`, `" | more"`, `'" "extra-arg'` or an embedded newline → `422` on the equality check, and even a match would be a single argv element, never a shell string;
* exactly one `subprocess.Popen` call site in the whole application, list argv, `shell=False`, `stdin=DEVNULL`, `CREATE_NO_WINDOW`, a 30-minute timeout and a 4,000-line output cap;
* only three fixed filenames under a fixed `update\` subdirectory are ever discovered — no `..`, no caller-named executable;
* refuses with `409` while anything is listening on a ComfyUI port;
* no scheduler, timer, or startup path references it — `run_updater` is called from exactly two files, and `core/__init__.py` (the auto-reindex scheduler) does not import `comfyui_service`;
* not exposed as an MCP tool, and `tools/list` contains no `update`/`run_updater` string.

**One weakness, S-04 (Medium):** the confirmed path is derived from `comfyui_path`, which the API accepts on a weak install check. The confirmation is honest about *what* will run; it is the *which install* question that is under-validated. Fix per S-04 and the surface is sound.

---

## 9. Sign-off

**Not signed off.** Two High findings are open: **S-01** (junction escape, `backend-core`) and **S-02** (MCP CSRF, `mcp`, plus a `MCP_SPEC §9` amendment from the architect). Both have a concrete, small fix and a test already written and marked `xfail` — flipping those two tests to passing is the sign-off condition. The five Mediums should land in the same pass; the Lows and the dependency floors can follow.

`backend/tests/security/` — 8 files, 343 passing assertions, `ruff` clean, hermetic, safe to run against a machine holding the real library.

*Geekatplay — Vladimir Chopine*
