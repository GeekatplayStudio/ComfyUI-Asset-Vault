# Security Review — Geekatplay ComfyUI Asset Vault v2.0

**Reviewer:** `security` agent · **Date:** 2026-08-22 · **Remediation pass:** 2026-08-23 · **Launcher pass:** 2026-08-23 (§4.1, §8.1) · **Scope:** `backend/app/**`, `frontend/src/**`, `backend/requirements.txt`
**Method:** static review plus **execution**. Every finding below was reproduced by running code, not by reading it — and every fix was proved by re-running that reproduction.
**Test suite:** `backend/tests` — **1,642 passed, 4 skipped, 0 xfailed**, `ruff check backend` clean. Every finding that carried an `xfail` has been either fixed and the marker replaced by a regression comment, or left `xfail` with a recorded won't-fix reason. There are currently **none of the latter**.
**Data safety:** every test is hermetic (synthetic ComfyUI tree in `tmp_path`, throw-away `vault.db`). The owner's library was verified unchanged after the audit, the remediation pass and the launcher pass: **a real-world library of several hundred models and thousands of outputs**, trash empty. The launcher pass additionally verified that the install's parent folder still holds exactly its four original `run_*.bat` scripts and that nothing was written into the ComfyUI install — **the owner's ComfyUI was never started by this review**, and every process it did start was a throw-away batch file in `tmp_path` that wrote a marker and exited.

---

## 1. Verdict

| Severity | Raised | Fixed | Open |
|---|---|---|---|
| **Critical** | 0 | — | 0 |
| **High** | 2 | **2** | 0 |
| **Medium** | 7 | **7** | 0 |
| **Low** | 10 | **4** (S-09, S-21 … S-23) | 6 (S-10 … S-14, S-24 — hardening) |
| **Informational** | 4 | — | 4 (accepted) |
| **QA defects** | 2 | **2** | 0 |

**Both Highs are closed**, and so is every Medium. Neither High was a flaw in the C5 grant — both were gaps in controls the plan already required (`BUILD_PLAN §7.1` junctions, `§7.6` CSRF). The six remaining Lows are hardening items with no demonstrated exploit; they are listed with their evidence and left open deliberately.

**S-19 … S-24 were added by the launcher pass on 2026-08-23** (§4.1, verdict in §8.1), which closed the review's own open note: §8 had recorded that the second `subprocess.Popen` call site — the "Open in ComfyUI" launcher — had not been reviewed. Two of the six are Medium and both were reproduced by *actually starting a process the owner did not choose*, using a staged batch file that writes a marker and exits.

What the audit could **not** break, having tried by execution:
* no path traversal escaped a root through any endpoint — 13 hostile folder specs, 8 hostile filenames, absolute paths, `\\?\`, UNC, `%2e%2e`, drive switching, `.. ` / `...` / `.. .` trailing-dot-and-space variants, and >260-character paths were all contained;
* no SQL injection — 12 payloads × 5 list surfaces × sort/group, plus the new `/storage` queries; the schema was intact afterwards;
* no code execution on the parsing path — a `__reduce__` pickle bomb, a `!!python/object/apply` YAML, and a `custom_nodes/__init__.py` that calls `os.system` were all parsed as inert data;
* no CSRF on any of the 43 mutating v1 routes;
* the `civitai_api_key` was never returned, logged, or surfaced through MCP;
* the C8 updater could not be run unconfirmed, could not have arguments injected, and is not reachable from MCP;
* the C8 launcher could not be redirected by any confirmation — 9 hostile `confirm_launcher_path` shapes and 5 spelling variants of the real path, a junction, a stale confirmation and 5 hostile `launcher` ids all left the argv as the path discovery chose, or refused outright.

---

## 2. Finding table

| ID | Severity | Title | Location | Owner | Status |
|---|---|---|---|---|---|
| **S-01** | **High** | NTFS junctions let the indexing walker read and index files outside every configured root | `app/indexing/walker.py:74,79`, `app/parsers/node_ast.py:164` | `backend` | **Fixed** — 2026-08-22 |
| **S-02** | **High** | `/api/v1/mcp` accepts a browser simple request, so any loopback-origin page can drive the destructive tool surface | `app/mcp/http.py:145`, `app/main.py:126` | `mcp` (+ architect: `MCP_SPEC §9`) | **Fixed** — 2026-08-22 |
| **S-03** | Medium | No SSE subscriber cap — `ProgressBus._subs` is unbounded | `app/core/progress.py:26,83` | `backend` | **Fixed** — 2026-08-23 |
| **S-04** | Medium | The updater executable follows `comfyui_path`, which the API accepts, so any directory holding `update\update_comfyui.bat` becomes the confirmed updater | `app/services/comfyui_service.py:240-252`, `app/api/v1/system_router.py:123` | `API layer` + `backend` | **Fixed** — 2026-08-23 |
| **S-05** | Medium | No `Image.MAX_IMAGE_PIXELS` budget and no `formats=` allowlist — a 324-byte PNG allocates ~480 MB | `app/jobs/thumb_service.py:258-267`, `app/parsers/image_meta.py:143` | `backend` | **Fixed** — 2026-08-23 |
| **S-06** | Medium | Unlimited MCP session creation defeats the per-session rate limit | `app/mcp/protocol.py:161-173` | `mcp` | **Fixed** — 2026-08-23 |
| **S-07** | Medium | No request body size cap — a 64 MB body is fully buffered and parsed before rejection | `app/api/middleware.py`, `app/mcp/http.py:145` | `API layer` | **Fixed** — 2026-08-23 |
| **S-08** | Medium | SSRF: `/system/ollama/test` and the `ollama_url` config key accept any absolute URL | `app/api/v1/system_router.py:460`, `app/services/ollama_service.py:28` | `API layer` + `backend` | **Fixed** — 2026-08-23 |
| **S-09** | Low | Dependency floors admit vulnerable builds (`pillow>=10.3`, `python-multipart>=0.0.6`, no explicit `starlette` pin) | `backend/requirements.txt` | `backend` | **Fixed** — 2026-08-23 |
| **S-10** | Low | `POST /system/roots` accepts any existing directory, including `C:\Windows`, widening the root guard with no warning | `app/api/v1/system_router.py:403` | `API layer` | Open |
| **S-11** | Low | `shutil.rmtree` in the permanent-delete path is gated on `os.path.isdir`, not on `kind == "node_package"` | `app/services/file_ops.py:412-414` | `backend` | Open |
| **S-12** | Low | `trash_empty` calls `rmtree(force=True)` on a DB-supplied path without re-validating it is inside a root's `.vault-trash` | `app/services/file_ops.py:915-940` | `backend` | Open |
| **S-13** | Low | `torch_zip` caps read size but not archive entry count | `app/parsers/torch_zip.py:70-83` | `backend` | Open |
| **S-14** | Low | `/system/validate-path` caps its models/output/input walk but not its `walk_json` workflow count | `app/api/v1/system_router.py:170-175` | `API layer` | Open |
| **S-15** | Info | `keep_extension` rewrites `new_name` before validation, so a rejected-looking name silently becomes a different one | `app/services/file_ops.py:290-294` | `backend` | Accepted |
| **S-16** | Info | The safetensors header cap is 200 MB; `BUILD_PLAN §7.12` says 100 MB | `app/parsers/safetensors_header.py:19` | `backend` | Accepted |
| **S-17** | Info | The app's own `data/` directory is a configured root, so file operations can target it | `app/core/config_service.py` | `backend` | Accepted |
| **S-18** | Info | `embed_service` downloads over `follow_redirects=True` with no host allowlist, no expected hash, and no free-space check | `app/jobs/embed_service.py:144-184` | `backend` | Accepted (precedent for C9 — see §5) |
| **S-19** | Medium | A `.bat` argv is re-parsed by `cmd.exe`, so a launcher **filename** containing `&` runs a second command the confirmation never named | `app/services/comfyui_service.py` (`_start_comfyui_thread`, `_run_updater_thread`) | `backend` | **Fixed** — 2026-08-23 |
| **S-20** | Medium | Launcher discovery follows `comfyui_path` with no install proof — S-04's shape, fixed for the updater and left open for the launcher | `app/services/comfyui_service.py:discover_launchers`, `app/api/v1/comfyui_router.py` | `backend` | **Fixed** — 2026-08-23 |
| **S-21** | Low | `run_*.bat` is globbed in the *parent* of the ComfyUI folder — a drive root on a portable build — and offered as an executable on filename alone | `app/services/comfyui_service.py:discover_launchers` | `backend` | **Fixed** — 2026-08-23 |
| **S-22** | Low | The workflow copy checks "inside a configured root" but not "inside the workflows folder", and used `shutil.copyfile` rather than an exclusive create | `app/services/comfyui_service.py:copy_into_user_workflows` | `backend` | **Fixed** — 2026-08-23 |
| **S-23** | Low | A `--port` outside 1–65535 in a launcher script raises `OverflowError` on the launch thread, stranding the launch state at `starting` and never closing the SSE stream | `app/services/comfyui_service.py:_port_open,_start_comfyui_thread` | `backend` | **Fixed** — 2026-08-23 |
| **S-24** | Low | A process launch is recorded only in memory; nothing the owner can read after a restart says the vault ever started ComfyUI | `app/services/comfyui_service.py:_launch_state` | `backend` | Open |
| **QA-3** | Defect | A node-class mapping dict defined in a sibling module under a non-standard name was not followed across the module boundary, so the registered `node_id` was lost | `app/parsers/node_ast.py` | `backend` | **Fixed** — 2026-08-23 |
| **QA-4** | Defect | `validate_filename` enforced no length limit, so a component over the NTFS 255-character maximum failed later as a raw `OSError` | `app/core/pathsafe.py:110` | `backend` | **Fixed** — 2026-08-23 |

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

**Recommendation** (owner: `backend`). In both walkers, treat any reparse point as a non-directory. `os.scandir` exposes it without an extra syscall:

```python
st = entry.stat(follow_symlinks=False)
if st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
    continue                      # junction, symlink, or mount point
```
or equivalently `entry.stat(follow_symlinks=False).st_reparse_tag != 0` on Python 3.12. Record a `scan_errors` row (`code='REPARSE_POINT_SKIPPED'`) so the skip is visible rather than silent, and add the same guard to `walker.top_level_dirs`. Flip `test_indexing_walker_does_not_descend_a_junction` and `test_node_scanner_does_not_descend_a_junction` from xfail to pass.

---

**Fixed 2026-08-22.** `walker.is_reparse_point()` treats any reparse point as a non-directory in both walkers (`indexing/walker.py`, `parsers/node_ast.walk_python_files`) using `st_reparse_tag` with an `st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT` fallback, and records the skip rather than swallowing it. `test_indexing_walker_does_not_descend_a_junction` and `test_node_scanner_does_not_descend_a_junction` are **hard regression gates** — they carry a comment saying a failure there is a reopened breach, and must never be marked `xfail` again.

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

`MCP_SPEC §9` currently names Origin validation as the anti-rebinding control and says nothing about CSRF. That statement is incomplete: **Origin validation stops DNS rebinding; it does not stop a same-machine loopback page.**

---

**Fixed 2026-08-22.** All three recommendations landed on `/api/v1/mcp`: a `Content-Type: application/json` guard answering `415`, the same `X-Vault-Request: 1` requirement every mutating v1 route enforces, and a narrowed `ORIGIN_OK`. `test_mcp_cannot_be_driven_by_a_browser_simple_request` is a **hard regression gate** for the same reason as S-01.


## 4. Medium and Low findings

### S-03 — No SSE subscriber cap (Medium, `backend`)
`ProgressBus._subs` is a plain list with no maximum; `/index/stream`, `/hash/stream`, `/embeddings/stream` and `/comfyui/update/stream` all append to it. Each subscriber holds an `asyncio.Queue(maxsize=1008)`, and every published event fans out across the whole list. `BUILD_PLAN §7.12` requires an SSE subscriber cap; there is none (`grep MAX progress.py` yields only `MAX_QUEUE`). **Fix:** add `MAX_SUBSCRIBERS` (32 is generous for a single-user desktop app) and refuse a new subscription with `503 FEATURE_UNAVAILABLE` past it. The per-subscriber `overflow` drop is already correct and should stay.

**Fixed 2026-08-23.** `progress.MAX_SUBSCRIBERS = 32` per channel, with `SubscriberLimitError` raised inside `ProgressBus.subscribe` and a cheap `has_capacity()` pre-check in `deps.require_stream_capacity`, called by all five SSE routes (`/index/stream`, `/hash/stream`, `/embeddings/stream`, `/enable/stream`, `/comfyui/update/stream`) before the response headers go out — so the refusal is a real `503 FEATURE_UNAVAILABLE` envelope rather than a broken stream. The bus-level raise stays as the hard limit for any future caller that skips the pre-check. The per-subscriber `overflow` drop is untouched. *Proved by execution* (`test_the_bus_refuses_the_subscription_past_the_cap`): 32 live subscribers are registered, `subscriber_count` stops at 32, `require_stream_capacity` raises with `http_status == 503`, and the 33rd `subscribe()` raises `SubscriberLimitError`.

### S-04 — The updater path follows caller-settable config (Medium, `API layer` + `backend`)
`discover_updaters` builds candidates as `<comfyui_path>.parent / "update" / update_comfyui.bat`. `PATCH /system/config` accepts any directory that merely contains `models/` and `main.py`. Proven by execution (`test_the_updater_must_live_under_the_verified_comfyui_install`): pointing config at a staged directory made `/comfyui/update/plan` resolve and offer that directory's `.bat` as the confirmed updater. The `confirm_path` equality check is intact — it just confirms *the attacker's* path.

This needs the CSRF header, so it is not remotely reachable; it is a privilege-expansion step for anything that already has same-origin or dev-origin access (see S-02, and CORS in dev). **Fix:** (a) require a stronger install proof before a path is accepted as a ComfyUI root (`comfyui_version.py` **and** `main.py` **and** `models/`, not `models/` plus either); (b) make a `comfyui_path` change invalidate any in-flight update plan, and have `/update/plan` return a short-lived opaque `plan_token` that `/update/run` must echo alongside `confirm_path`; (c) surface a "the updater path changed since you confirmed" warning in the UI. `test_changing_the_install_path_changes_what_would_run` already asserts that a stale confirmation is rejected — keep it.

**Fixed 2026-08-23.** Both updater routes now call `_require_verified_install()` in `comfyui_router` before anything is resolved or run: the configured folder must carry **`comfyui_version.py` *and* `main.py` *and* `models/`**. `comfyui_version.py` is generated by ComfyUI's own release process and is already what `comfyui_service.read_version` parses, so it is proof a staged directory does not get for free. The gate sits in the router rather than in `comfyui_service.py` — that file was under concurrent edit for the launch feature and was deliberately left alone. `/update/run` is gated independently of `/update/plan`, so a stale confirmation cannot skip the check. `PATCH /system/config` still accepts the weaker `models/` + `main.py` shape, because that is what the wizard and the indexer legitimately work with; what changed is that such a folder is no longer allowed to nominate an executable.

*Proved by execution* (`test_the_updater_must_live_under_the_verified_comfyui_install`): the staged `AttackerStaging\ComfyUI` + `AttackerStaging\update\update_comfyui.bat` layout that previously produced a confirmed updater now yields `404 NOT_FOUND` with `details.missing == ["comfyui_version.py"]` from `/update/plan`, and `404` from `/update/run` for the same batch file. `test_changing_the_install_path_changes_what_would_run` keeps asserting that a stale confirmation is refused; its two fixtures were given a `comfyui_version.py` so they remain *verified* installs and keep testing what they were written to test.

**Not done, deliberately.** The recommended short-lived opaque `plan_token` was not added. The equality check on `confirm_path` already refuses a stale confirmation (asserted), and a token would add a second piece of server state to the one route in the app that starts a process — more moving parts guarding an entry that is now behind an install proof, a running-port check, a verbatim path echo and the CSRF header.

### S-05 — Image decompression bombs (Medium, `backend`)
`Image.MAX_IMAGE_PIXELS` is never set, so Pillow's default applies: a `DecompressionBombWarning` at 89 Mpx and a hard error only at 178 Mpx. Executed: a **324-byte** PNG declaring 20000×8000 loaded successfully and allocated ~480 MB. `thumb_service._generate` calls `im.load()` (a full decode), sets `ImageFile.LOAD_TRUNCATED_IMAGES = True` globally, and passes no `formats=` allowlist to `Image.open` — so the PSD, FITS, JPEG2000 and raw-codec paths are live on files the vault did not write. `image_meta.read_image` opens from a path string with the same exposure.

The endpoint degrades correctly (a bomb over the hard limit yields a placeholder, not a 5xx — asserted), so this is memory pressure, not a crash. **Fix:** set `Image.MAX_IMAGE_PIXELS = 64_000_000` explicitly in `thumb_service` and `image_meta`, pass `formats=["PNG","JPEG","WEBP","GIF","BMP","TIFF"]` to both `Image.open` sites, and record an integrity note rather than a silent placeholder when the budget is exceeded.

**Fixed 2026-08-23.** New module `app/core/imaging.py` holds the budget and the allowlist, and both `Image.open` owners apply it at import (`thumb_service`, `image_meta`):

* `MAX_IMAGE_PIXELS = 64_000_000`, pinned explicitly rather than inherited;
* `exceeds_budget()` is checked against the *header* dimensions immediately after `Image.open` and before any `load()`, so an over-budget file costs a header read. In `image_meta` that writes a `scan_errors` row naming the dimensions and the budget — the integrity note the recommendation asked for — instead of a silent placeholder; in `thumb_service` it falls through to the placeholder path the endpoint already had;
* `formats=` is passed at all four `Image.open` sites: the full allowlist for source files, a PNG-only list for the client-supplied 3D poster, and a decoder-frame list for the ffmpeg frame.

**One trap worth recording.** `formats=` must be filtered through `Image.OPEN` first. Pillow does `OPEN[name]` inside `_open_core` and re-raises anything that is not `SyntaxError`/`IndexError`/`TypeError`/`struct.error`, so a name the local build has not registered (AVIF on a build without it) turns *every unrecognised file* into a bare `KeyError` out of `Image.open` rather than a clean `UnidentifiedImageError`. `imaging.open_formats()` does that filtering and caches the result.

*Proved by execution*: the 321-byte PNG declaring 20000×8000 is now refused by `Image.open` itself (`DecompressionBombError`, no decode, no allocation); a 207-byte PNG declaring 10000×9000 — between the new budget and Pillow's own 2× hard stop — is caught by `exceeds_budget` and recorded as a scan error with `has_metadata` still false (`test_an_over_budget_header_is_refused_before_the_decode`); a PSD magic number is refused by the allowlist with a clean `UnidentifiedImageError`; and `test_a_bomb_never_turns_a_thumbnail_request_into_a_5xx` still passes, so the endpoint degrades exactly as before.

### S-06 — Unlimited MCP sessions (Medium, `mcp`)
`SessionStore.create` sweeps expired sessions but has no maximum. Executed: 300 `initialize` calls produced 300 live sessions. The 120-call/minute rate limit is per-session, so rotating sessions removes it entirely, and each session is retained memory. **Fix:** cap the store (64 is ample), evict least-recently-used past the cap, and add a per-transport call budget alongside the per-session one.

**Fixed 2026-08-23.** `SessionStore` gained `MAX_SESSIONS = 64` and `_evict_to()`, which drops **least-recently-seen** sessions on `create()` after the expiry sweep. Evicting the oldest is what makes the cap safe to apply: a client that is actually working keeps its session, and only abandoned ones are reclaimed. *Proved by execution* (`test_the_session_store_never_grows_past_its_cap`): 300 `initialize` calls leave `SESSIONS.count() <= 64`, and the most recent session is still resolvable afterwards.

**Residual, recorded rather than papered over.** The cap fixes the unbounded-memory half of the finding. It does **not** fully fix the rate-limit half: a client can still rotate sessions to get a fresh 120-call budget, it just cannot accumulate them. The recommended per-transport call budget was **not** added, and that is a deliberate call — a process-wide counter over a shared in-process store would throttle legitimate concurrent clients (and the suite's own MCP traffic) against a limit that exists to bound one misbehaving client. The rotation is now bounded by session-creation cost, and since S-02 `/api/v1/mcp` requires `X-Vault-Request`, so it is not reachable from a browser page at all. If this is revisited, a rate limit on `initialize` — rather than on tool calls — is the shape that would not penalise real clients.

### S-07 — No request body cap (Medium, `API layer`)
Neither uvicorn nor the app imposes a `Content-Length` limit. A 64 MB JSON body to `/api/v1/fileops/rename` was fully read and parsed before Pydantic's `max_length=255` rejected the field; `/api/v1/mcp` does `await request.body()` with no bound at all. `BUILD_PLAN §7.12` requires JSON size caps. **Fix:** an ASGI middleware that rejects `Content-Length` above 2 MB (and streams-with-no-length above the same budget) with `413 PAYLOAD_TOO_LARGE`, exempting nothing — no v1 endpoint has a legitimate multi-megabyte body.

**Fixed 2026-08-23.** `BodySizeLimitMiddleware` (pure ASGI, in `app/api/middleware.py`) is installed inside `RequestContextMiddleware`, so a refusal still carries `X-Request-Id` and `X-API-Version`. An over-large `Content-Length` is answered without reading a byte off the wire; a chunked body that declares no length is counted as it arrives and aborted the moment it crosses the budget, so omitting the header is not a bypass. The streaming abort raises a Starlette `HTTPException(413)` rather than an `ApiError`, because FastAPI's body reader re-raises `HTTPException` unchanged and rewrites everything else into a generic 400.

`MAX_BODY_BYTES = 8 MB`, not the recommended 2 MB, and the reason is in the code: `POST /files/thumbnail/rendered` legitimately carries `thumb_service.MAX_RENDERED_BYTES` (4 MB) of PNG as a base64 data URL, which is ~5.4 MB inside a JSON envelope. Nothing is exempted — the cap applies to every route including `/api/v1/mcp` — and `test_the_body_cap_leaves_room_for_the_largest_legitimate_body` pins the relationship between the two numbers so they cannot drift apart silently.

*Proved by execution*: a 32 MB body to `/fileops/rename` → `413 PAYLOAD_TOO_LARGE`; the same payload streamed in 64 KB chunks with no `Content-Length` → `413`; a 32 MB body to `/api/v1/mcp`, which reads `await request.body()` outside any Pydantic model → `413`; an ordinary body is unaffected.

### S-08 — SSRF through the Ollama URL (Medium, `API layer` + `backend`)
Executed: with `ollama_enabled` true (settable in the same session), `POST /system/ollama/test {"url": "http://127.0.0.1:<port>"}` produced a real outbound GET to the caller-chosen host and port, and the response body reported `available`, the HTTP status and the latency — a working host/port scanner. Worse, `PATCH /system/config {"ollama_url": ...}` **persists** an arbitrary URL, and `POST /ai/describe` sends the asset fact sheet — including a workflow's `positive_prompt` — to it. That is a data-exfiltration sink, not just a probe. **Fix:** validate both `OllamaTestRequest.url` and the `ollama_url` config key against `http(s)://(localhost|127.0.0.1|[::1])(:port)?` — Ollama is a local service by definition — and reject anything else with `422`. If a LAN Ollama must be supported, gate it behind the same explicit `ALLOW_LAN` opt-in the bind uses.

**Fixed 2026-08-23**, with a deliberately conservative rule in the new `app/core/urlsafe.py`, applied in two places:

1. **At the schema boundary** — `OllamaTestRequest.url`, `ConfigPatch.ollama_url` and `WizardCompleteRequest.ollama_url` all run `check_local_url`, so a bad address is a `422` naming the offending host and it never reaches the database.
2. **At the point of use** — `OllamaService._checked_base_url()` re-validates before `check_connection`, `list_models` and `generate`. The value also arrives from `vault.db` and from a hand-edited config file, and `generate` is what carries the owner's workflow text, so it is checked because it is about to be used, not because it was once stored.

**The rule, and why it is drawn there.** Loopback **and the RFC1918 private ranges are allowed with no ceremony** — running Ollama on another machine on the home LAN is a real setup, and breaking it would be a worse outcome than the finding. Refused: link-local (`169.254.0.0/16`, `fe80::/10` — the cloud metadata endpoint), public addresses, credentials in the authority, a path/query/fragment, and any non-`http(s)` scheme. **Names other than `localhost` are refused too**, so DNS is not in the trust path: a resolve-then-check rule would still lose to rebinding between the check and the request, and a LAN Ollama is addressed by IP. The allowed networks are written out literally rather than taken from `ip_address.is_private`, whose membership (0.0.0.0/8, 100.64.0.0/10, 240.0.0.0/4) has changed between CPython releases. If a hostname is ever genuinely needed, `urlsafe.py` is where an explicit per-host opt-in belongs — never a silent widening.

*Proved by execution* (`test_no_endpoint_accepts_a_non_local_ollama_address`, 9 payloads × 2 surfaces): `http://169.254.169.254`, a public address, a bare name, credentials in the authority, a URL with a path, `file:///`, IPv6 link-local and `0.0.0.0` are each `422` on both the probe and the config key, and the stored value is unchanged. `test_a_lan_ollama_is_still_accepted` proves the six loopback/LAN shapes still return `200`. `test_the_service_refuses_a_non_local_url_that_reached_the_database` drives `OllamaService("http://169.254.169.254")` directly: `check_connection` reports the refusal and `generate` returns `ok: False` with no text — no socket is opened either way.

### S-09 — Dependency floors (Low, `backend`)
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

**Fixed 2026-08-23.** `backend/requirements.txt` now reads:

```
fastapi>=0.135          # so the starlette floor below is satisfiable
starlette>=1.3.1        # SECURITY_REVIEW S-09: CVE-2026-48818 (UNC/NTLM via StaticFiles)
pydantic>=2.4.0         # SECURITY_REVIEW S-09: CVE-2024-3772 (ReDoS), free to fix
pillow>=12.3.0          # SECURITY_REVIEW S-09: 17 CVEs reachable through Image.open
# python-multipart removed: no UploadFile/File(/Form(/request.form() anywhere in
# app/, so it was an unused dependency carrying nine advisories (S-09).
```

`python-multipart` was **removed, not pinned**. Verified unused before removal: `UploadFile`, `File(`, `Form(` and `request.form()` have no call site anywhere under `app/` — the only two `grep` hits for "multipart" are a comment in `mcp/http.py` about which content types a browser can send, and an unrelated `zipfile.ZipFile` line. Its transitive install stays in the venv (0.0.32) because `uvicorn[standard]`/FastAPI extras pull it, but nothing declares it.

*Proved by execution*: `pip install --dry-run -r requirements.txt` resolves with no conflict and nothing to install against the existing venv (fastapi 0.141.1, starlette 1.6.0, pydantic 2.13.4, pillow 12.3.0), and `test_pillow_floor_excludes_known_cves` / `test_python_multipart_is_not_pinned_at_a_vulnerable_floor` are now hard gates rather than `xfail`.
`PyYAML`, `onnxruntime`, `tokenizers`, `uvicorn`, `httpx` and `numpy` need no change. **All three D9 dependencies are justified:** `PyYAML` for `extra_model_paths.yaml` (C2/D4, `safe_load` only), `onnxruntime` + `tokenizers` for the CPU-only local embedder that exists specifically to avoid a torch dependency (C2) — and keeping torch out of the tree is itself the single largest reduction in this app's attack surface.

### S-10 — `POST /system/roots` accepts any directory (Low, `API layer`)
Executed: `{"path": "C:\\Windows", "kind": "extra_workflows"}` returned `201`. That directory becomes a scan root and therefore a `resolve_within_roots` root, so anything indexed under it becomes a legitimate file-operation target. It is a deliberate user action behind the CSRF header and no MCP tool exposes it, hence Low — but "roots enforced" is only as strong as what may become a root. **Fix:** refuse system directories (`%WINDIR%`, `%PROGRAMFILES%`, `%SYSTEMROOT%`, a bare drive root), require the directory to contain at least one `.json` workflow, and return a `warnings[]` array the UI must display before the root is saved.

### S-11 — `rmtree` is gated on `isdir`, not on kind (Low, `backend`)
`file_ops.delete` chooses `shutil.rmtree` from `is_dir = os.path.isdir(long_path(old))`. The only uid kind that should ever resolve to a directory is `node_package` (`DIRECTORY_KINDS` exists and says so), but the delete path does not consult it. A `models`/`outputs` row whose `abs_path` ever pointed at a directory would be recursively deleted. **Fix:** `if is_dir and info["kind"] not in DIRECTORY_KINDS: raise ValidationError(...)` before the `rmtree`, so the recursive branch is reachable only through the one deliberate case.

The deliberate case itself was checked hard and is sound: it is trash-backed by default (a same-volume `shutil.move`, O(1)), captures the `node_classes` rows for an exact restore, warns on a dirty git checkout, and refuses rename/move on a package with a specific displayable reason.

### S-12 — `trash_empty` rmtree is not re-validated (Low, `backend`)
`_purge_slot(slot, force=True)` calls `shutil.rmtree(ignore_errors=True)` on `dirname(trash_items.trash_path)` straight from the database, with no containment check. The value is only ever written by `_to_trash` under `<root>/.vault-trash/`, so it is not attacker-controlled today — but it is the one `rmtree` in the codebase with no guard in front of it, and it runs on startup via `purge_expired()`. **Fix:** assert `is_contained(slot, root.path)` **and** that the path's parent basename is `TRASH_DIRNAME` before the `rmtree`, and skip with a logged warning otherwise.

### S-13 — `torch_zip` has no entry-count cap (Low, `backend`)
Read size is capped correctly (`MAX_PICKLE = 32 MB`, checked against the *declared* uncompressed size, so a zip bomb is refused in 7 ms — verified). `zf.namelist()` is unbounded: a 20,000-entry archive parsed in 152 ms, but the whole central directory is materialised in memory. `BUILD_PLAN §7.13` asks for an entry cap. **Fix:** refuse above 100,000 entries with `integrity='unsupported_format'`. Zip-slip is already a non-issue — nothing is ever extracted to disk (verified).

### S-14 — `/system/validate-path` workflow walk is uncapped (Low, `API layer`)
`_bounded_preview` enforces 60,000 entries and a 4-second deadline on the models/output/input walk, then falls through to `sum(1 for _ in walker.walk_json(directory))` for two workflow directories with **no cap and no deadline**. **Fix:** move the workflow count inside the same budget.

---

### QA-3 — A cross-module node mapping lost the registered `node_id` (Defect, `backend`)

**Fixed 2026-08-23.** `collect_mappings` only absorbed a dict once it could see it merged into `NODE_CLASS_MAPPINGS` *in the same module*. The common packaging idiom puts the dicts in `nodes_a.py` under a name of the author's choosing (`MAPPINGS_A = {...}`) and merges them in `__init__.py`, so the name stayed unresolved. The classes were still recovered by the S5 structural fallback, but keyed on the **Python class name** instead of the registered node_id — and a workflow references `class_type`, which is the node_id. A package indexed that way reads as a set of phantom missing dependencies.

New `collect_exported_dicts()` exposes every module-level dict literal, flattened to node_id/class pairs; `extract_package` records the unresolved reference names per module, resolves each through the module's own import table (`collect_imports` → `_resolve_module`), and absorbs the pairs as S2. It reads one extra module, cached, and reads no code it was not already going to parse — the absolute no-import/no-exec rule is untouched.

*Proved by execution* (`test_s2_preserves_the_registered_node_id_across_modules`): the `pkg_s2` fixture — four merge idioms across `__init__.py`, `nodes_a.py` and `nodes_b.py` — now yields `ProbeAlpha`, `ProbeBeta`, `ProbeGamma` and `ProbeDelta` rather than `AlphaNode`…`DeltaNode`. The sibling `test_s2_augmenting_assignment_recovers_every_class` still passes, so nothing regressed on the class-name side.

### QA-4 — `validate_filename` had no length limit (Defect, `backend`)

**Fixed 2026-08-23.** `pathsafe.MAX_COMPONENT_CHARS = 255` (the NTFS single-component maximum) is now enforced in `validate_filename`, so an over-long name is a `422` with a message the user can act on rather than a raw `OSError` surfacing from whichever syscall runs first. That check is also the only thing standing between a long name and a partially created folder tree: the API schema caps rename at 255, but `file_ops.create_folder` and the workflow-save route passed unbounded path components straight through.

*Proved by execution* (`test_names_over_the_filesystem_limit_are_rejected`): a 300-character name plus extension raises `ValidationError`; the 40-odd ordinary names in `test_ordinary_names_are_accepted` are unaffected, so the validator did not start failing closed on normal input.

---

## 4.1 The launcher pass — S-19 … S-24 (2026-08-23)

Raised by the follow-up review §8 asked for. All six were reproduced by execution against a
synthetic portable install in `tmp_path`; **the owner's install was never written to and
ComfyUI was never started.** The two Mediums were proved by letting a *staged* launcher — a
`.bat` that writes a marker file and exits — actually run, which is the only way to tell
"the gate is missing" apart from "the gate is missing but nothing lands".

### S-19 — a `.bat` argv is re-parsed by `cmd.exe` (Medium, `backend`)

**Location** `app/services/comfyui_service.py` — both `Popen` call sites.

**What is wrong.** Both call sites build a list argv with `shell=False`, and §8 recorded that
as the control that makes the argv inert. On Windows it is not, for a batch target.
`CreateProcess` cannot execute a `.bat`/`.cmd` directly: it runs it through `cmd.exe`, which
re-parses **the whole command line** — including the executable's own path. And
`subprocess.list2cmdline` quotes a token only when it contains a space or a tab, so a
metacharacter in an otherwise space-free path is handed to the interpreter live.

The launcher is exposed to this in a way the updater is not: the updater's filenames are three
fixed literals under a fixed `update\` subdirectory, while the launcher's name is whatever
matched `run_*.bat`. `&` is a perfectly legal NTFS filename character.

**Reproduction** (executed; now `backend/tests/security/test_launcher.py::test_a_cmd_metacharacter_in_the_launcher_name_starts_nothing`):

```
<install>\run_a&injected.bat      # a benign launcher, as far as the dialog shows
<somewhere on PATH>\injected.bat  # writes a marker
GET  /comfyui/open-workflow/plan  -> launcher_confirm_path = ...\run_a&injected.bat
POST /comfyui/open-workflow {start:true, confirm_launcher_path: <that exact path>}
```

Observed, before the fix: `cmd.exe` split the line at `&`, reported `'...\run_a' is not
recognized`, then ran `injected.bat` — **the marker file was written** — and the launch state
reported `exit_code: 0`. The confirmation dialog had named one file; two commands ran.

The same reproduction on `Popen(<the same path>, cwd=...)` with the command line explicitly
quoted runs the batch file correctly and writes no marker, which is what identifies quoting —
not the list form — as the thing that was actually protecting anyone.

**Impact.** A confirmation bypass at the one place the app starts a process. The precondition
is write access to the folder beside the ComfyUI install, which on the owner's portable build
is a drive root — reachable by an archive extracted one level too high or by any
installer. It is not remote: `/comfyui/open-workflow` is behind the CSRF header, and the owner
still has to answer the start question. Medium, not High, for that reason.

**Fixed 2026-08-23.** `cmd_line_hazard(command)` returns the character `cmd.exe` would act on,
or `None`. It applies only to `.bat`/`.cmd` targets — a real executable never reaches an
interpreter — and it is deliberately not a flat blacklist: `"` and `%` survive quoting and are
always refused, while `& | < > ^ ( ) !` are refused only when `list2cmdline` leaves the token
unquoted. That distinction is the whole reason `C:\Program Files (x86)\...\run_nvidia_gpu.bat`
still launches: it contains a space, so it is quoted, so nothing in it is parsed. The check
runs in `discover_updaters` and `discover_launchers` (the entry is listed with
`available: false` and `unsafe_reason: "cmd_metacharacter_in_path"`, so the UI can say why),
and again in `run_updater` and `start_comfyui` immediately before the spawn, because what
reaches `Popen` is what matters.

*Proved by execution*: five hostile names (`&`, `&&`, `^`, `(`, `%`) are each refused with
`409`, `subprocess.Popen` is never reached, the payload sitting on `PATH` never runs and its
marker never appears; a parenthesised install path with a space still resolves, still reports
`available: true`, and `list2cmdline` still quotes it; an updater under a parent directory
named `Portable&injected` is refused by `/update/plan` with `409`.

### S-20 — the launcher followed the configured path with no install proof (Medium, `backend`)

**What is wrong.** Exactly S-04's shape, on the surface S-04's fix did not cover. `PATCH
/system/config` accepts any directory holding `models/` plus `main.py` *or* `nodes.py`.
`_require_verified_install` raised that bar to `comfyui_version.py` + `main.py` + `models/` —
but only on the two updater routes. `discover_launchers` reads the same `comfyui_path`,
globs its parent, and `/comfyui/open-workflow/plan` and `/comfyui/open-workflow` call it with
no gate at all.

**Reproduction** (executed; now `test_the_launcher_must_live_under_a_verified_comfyui_install`):

```
<tmp>\AttackerStaging\ComfyUI\models\, main.py     # no comfyui_version.py
<tmp>\AttackerStaging\run_staged.bat               # writes a marker
PATCH /system/config {"comfyui_path": "<tmp>\AttackerStaging\ComfyUI"}   -> 200
GET   /comfyui/info      -> launchers[0].path = ...\run_staged.bat, recommended: true
POST  /comfyui/open-workflow {start:true, confirm_launcher_path: <that path>}
```

Observed, before the fix: the staged batch file was resolved, accepted as the confirmed
launcher, and **executed** — the marker file was written, `pid` was reported, and the launch
state moved to `failed` only because nothing then answered on port 8188. The confirmation
check was intact throughout; it was confirming the attacker's path.

**Fixed 2026-08-23.** The rule moved out of the router and into `comfyui_service` as
`INSTALL_PROOF` / `missing_install_proof()`, and `discover_launchers` returns an empty list
for a folder that fails it. The router's `_require_verified_install` now delegates to the same
function, so the updater and the launcher cannot drift apart — asserted by
`test_the_updater_and_the_launcher_use_one_definition_of_a_real_install`. `resolve_launcher`
reports `details.missing`, so the plan can say *why* it offered nothing rather than shrugging.

*Proved by execution*: the staging layout above now yields `launchers: []`,
`recommended_launcher: null`, `details.missing == ["comfyui_version.py"]`, a `404` from
`/comfyui/open-workflow`, no `Popen` call, and no marker file. The plan route still answers
`200` with `launcher: null`, `can_open: false`, `blocked_reason: "no_launcher_found"` and a
`launcher_error` — a workflow's deep-link and copy information is still useful when the
install cannot be started, so the whole route does not have to 404.

### S-21 — a file is not a launcher because of its name (Low, `backend`)

`LAUNCHER_GLOB = "run_*.bat"` is matched in the **parent** of the ComfyUI folder. On the
owner's portable build that parent is a drive root; the four real launchers sit there
beside `ComfyUI\`, `python_embeded\` and `update\`. Any batch file that lands in that folder
and happens to start with `run_` was offered as "a way to start ComfyUI", with a resolved
absolute path, in a confirmation dialog — on the strength of its filename and nothing else.
A file dropped *inside* the ComfyUI folder is not affected: the glob never looks there, which
was checked (`test_only_the_parent_folder_is_globbed_not_the_install_tree`).

**Fixed 2026-08-23.** A script is offered only if its own text names ComfyUI's entry point,
and each entry reports the `evidence` it was accepted on. All four of the owner's launchers —
including the hand-written `run_nvidia_gpu_stable_memory.bat`, which a name allowlist would
have thrown away — carry `.\python_embeded\python.exe -s ComfyUI\main.py` and are unaffected.
This is deliberately **not** claimed as proof of intent: a hostile script can name `main.py`
in a comment. It is one layer of four, alongside the verified-install gate, the metacharacter
gate and the verbatim path confirmation, and it is what stops a file that never had anything
to do with ComfyUI from being nominated at all.

*Proved by execution*: a `run_setup.bat` staged beside a verified install is absent from
`probe().launchers`, is a `404` by id, is a `422` by path, never reaches `Popen`, and never
writes its marker; the genuine `run_nvidia_gpu.bat` beside it is unaffected. Discovery is
also asserted repeatable and order-stable, and the preferred name still wins over alphabetical
order, so a `run_aaa.bat` landing in the folder does not become the recommended launcher.

### S-22 — the copy was contained by the wrong boundary (Low, `backend`)

`copy_into_user_workflows` validated the filename and resolved the destination's parent
through `resolve_within_roots` — the vault-wide rule — but never checked the narrower thing
the route actually promises, that the file lands in `<comfyui>\user\default\workflows`.
Found by a test that poisoned the plan with `...\workflows\..\..\escaped.json`: the write
succeeded into `<comfyui>\user\`, inside a configured root and outside the advertised folder.
The destination is server-derived, so this is not reachable through the API today — it is the
second line of defence not being where the route's own promise is.

**Fixed 2026-08-23.** `is_contained(destination.parent, user_workflows_dir())` is asserted
after normalisation (so `..` is already collapsed and any junction already resolved), and the
copy became an exclusive create (`open(..., "xb")` + `copyfileobj`) instead of
`shutil.copyfile` — the existence check stays, because it produces a better message, but it is
no longer the thing standing between "it never overwrites" and a file that appears in between.

*Proved by execution*: six hostile destinations (`..\..`, an absolute path, an alternate data
stream, `CON.json`, a trailing dot-and-space, and a 300-character name) are each refused with
the workflows folder byte-identical afterwards; a file created between the plan and the write
produces a `409` and keeps its original bytes.

### S-23 — an unusable port stranded the launch subsystem (Low, `backend`)

`_PORT_ARG_RE` reads `--port` out of the launcher script and `_port_open` passes it to
`socket.connect_ex`, which raises **`OverflowError`** — not `OSError` — for a value outside
0–65535. `_start_comfyui_thread` caught nothing around its wait loop, so the thread died with
`_launch_state["status"] == "starting"`: every later launch was then refused with "ComfyUI is
already being started", and the `done` event was never published, so `/comfyui/launch/stream`
subscribers were never released from the 32-slot S-03 budget. A launcher script saying
`--port 99999` was enough.

**Fixed 2026-08-23.** The port is range-checked at discovery and discarded if it is not a
port; `_port_open` also catches `OverflowError`/`ValueError`; and the wait loop is wrapped so
the terminal payload is published and the state leaves `starting` whatever happens. The three
fixes are deliberately redundant — the last one is the one that matters, because it holds for
the next unexpected exception too.

*Proved by execution*: `--port 99999` yields `port: None` and `_port_open(99999) is False`; a
`_port_open` that raises leaves the status `failed` with an error, and the **next** launch
succeeds rather than being refused by a stranded state machine.

### S-24 — a launch leaves no record the owner can read afterwards (Low, open)

`_launch_state` is a process-local dict. `/comfyui/launch/status` and the SSE stream expose
it while the app is up; nothing writes a row. After a restart there is nothing that says the
vault ever started a program — no table, no log line the UI surfaces. The updater has exactly
the same standing, so this is not a regression the launcher introduced; it is the gap the
launcher makes worth naming, because `mcp_audit` shows the project already knows how to record
a mutating act, and starting a process is a larger one than a rename.

**Left open, deliberately.** Adding a table is a schema change and a UI surface, not a
security fix that can be dropped in during a review pass. What is asserted now is the honest
current state: `test_a_launch_is_visible_to_the_owner_while_and_after_it_runs` pins that the
status carries the resolved path, the trigger and both timestamps while the process is alive,
**and** that no `launch_audit`/`process_audit` table exists — so the day one is added, the
test says so.

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
| 1 | Path traversal (`..`, `%2e%2e`, UNC, `\\?\`, ADS, 8.3, junctions/symlinks, drive switch, case, trailing dots/spaces, >260 chars) | **Pass.** S-01 fixed; a component over 255 characters is now a clean 422 as well (QA-4); 58 unit + 39 endpoint assertions |
| 2 | No client-supplied paths on any endpoint | **Pass.** Only `/system/config`, `/system/validate-path`, `/system/wizard/complete`, `/system/roots` (S-10) and the three echo-only confirmations — the updater's `confirm_path` and the launcher's `confirm_launcher_path` / `confirm_copy_destination`, each compared against a server-resolved path and never used as one (§8.1). `/api/outputs/file?path=` is gone and asserted gone |
| 3 | No code execution on the parsing path | **Pass.** No `import`/`exec`/`eval`/`compile`/`pickle`/`torch`/`subprocess` under `parsers`, `indexing`, `search`. `torch_zip` imports only `zipfile` + `pickletools`. Proven by execution against a live `__reduce__` bomb and a hostile `__init__.py` |
| 4 | YAML `safe_load` only | **Pass.** One call site; `!!python/object/apply` refused by execution; anchor bomb harmless |
| 5 | SQL injection | **Pass.** 127 assertions. `sort`/`group`/`reason` map through frozen dicts; the v0 `sort_column` shape is statically asserted absent, including in the new `reclaim_score` queries |
| 6 | CSRF | **Pass.** S-02 fixed: `/api/v1/mcp` now enforces `X-Vault-Request` and a JSON content type. All 43 mutating v1 routes enforce `X-Vault-Request`, walked from the live OpenAPI document |
| 7 | Bind posture, `ALLOW_LAN`, CORS | **Pass.** Loopback default; `SystemExit` on a LAN bind without `ALLOW_LAN=1`, logged loudly when set; MCP HTTP refuses to mount on the LAN without a token; CORS never `*`, never credentialed, loopback dev ports only |
| 8 | MCP posture | **Pass.** S-02 and S-06 both fixed; the session store is capped at 64 with least-recently-seen eviction. Origin validated, sessions terminable, 26 tools, no path/URL input, closed schemas, no file-content tool, no SSRF pivot, rate limited |
| 9 | Secrets | **Pass.** `civitai_api_key` never returned, never logged, never reachable through MCP; only `civitai_service` reads it and only `https://civitai.com/api/v1` receives it |
| 10 | Log hygiene | **Pass.** No traceback in any response body; `request_id` matches the header; no prompt or key reaches the log; MCP read tools log argument *keys* only, while mutations log values by C5 design |
| 11 | Delete safety | **Pass.** Permanent requires `confirm:true` on REST, storage cleanup and MCP; trash is the default and was proven reversible end-to-end; the one recursive delete is the deliberate node-package case (hardening: S-11, S-12) |
| 12 | DoS caps | **Pass.** All three gaps closed: SSE subscriber cap (S-03, 32/channel — `/comfyui/launch/stream` verified to inherit it, and to release its slot on `done` even when the launch fails, S-23), body size cap (S-07, 8 MB, `Content-Length` and streamed), image pixel budget (S-05, 64 Mpx plus a format allowlist). Header, GGUF, graph, AST, file-count, `limit` and batch caps unchanged |
| 13 | Zip/archive | **Pass with a gap.** No extraction, so no zip-slip; read size capped; entry count still uncapped (S-13, Low, open) |
| 14 | Dependency review | **Pass.** S-09 fixed: floors raised and the unused `python-multipart` removed. All three D9 dependencies justified; installed versions clean |

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

**S-04 (Medium) is fixed.** The confirmed path was derived from `comfyui_path`, which the API accepts on a weak install check: the confirmation was honest about *what* would run, but the *which install* question was under-validated. Both updater routes now require `comfyui_version.py` + `main.py` + `models/` before an updater is resolved or run. The surface is sound.

**S-19 also applies here.** The updater's argv is inert for a reason that turns out to be
narrower than §8 originally claimed: not "it is a list argv" but "its three filenames are
fixed literals and its directory is the owner's". A `.bat` command line *is* re-parsed by
`cmd.exe`, and an install parent named `Portable&injected` would have carried that through the
updater too. `discover_updaters` now refuses such a path, and `/update/plan` answers `409`.

## 8.1 Launcher verdict (C8, "Open in ComfyUI") — 2026-08-23

The note §8 left for whoever owned the launcher is closed. `services/comfyui_service.py`
holds **two** `subprocess.Popen` call sites — the updater and the ComfyUI launcher —
and `EXPECTED_POPEN_CALL_SITES = 2` is correct; the widening itself was legitimate. The
launcher's argv construction, confirmation gate, copy path, stream, reach and audit trail have
now had the treatment §8 gave the updater.

**The launcher is sound as it now stands, and it was not sound as it landed.** Two Medium
findings were real and both were *reproduced by starting a process the owner did not choose* —
S-20 (no verified-install gate, so a staged directory ran its own batch file) and S-19 (a `&`
in a launcher filename runs a second command past a confirmation that named one file). Both
are fixed and gated. Four Lows follow: S-21, S-22 and S-23 fixed, S-24 open.

What holds, verified by execution:

| Question | Verdict |
|---|---|
| Can a file the owner did not intend become the launcher? | **No, on four independent grounds.** The install must carry `comfyui_version.py` + `main.py` + `models/` (S-20); the script must name ComfyUI's entry point (S-21); the path must be inert to `cmd.exe` (S-19); and the resolved absolute path must be confirmed verbatim. A file dropped *inside* the ComfyUI folder — the custom-node case — was never in scope: the glob only ever looks in the parent |
| Is the argv injectable? | **No.** One element, taken from on-disk discovery, `shell=False`, `cwd` fixed to the launcher's own folder, `stdin=DEVNULL`, environment inherited rather than assembled. `uid`, `launcher` and `confirm_launcher_path` are each *matched*, never interpolated: five hostile `launcher` values and nine hostile `confirm_launcher_path` values all end in `404`/`422` with `Popen` never reached |
| Is the confirmation real? | **Yes, and it is structurally unable to redirect.** `confirm_path` is compared to the *resolved* path after `realpath`. Case variants, `\\?\`, `.` segments, forward slashes and 8.3 short names name the same file and are accepted — and the argv is still the discovered path, never the caller's string, which is asserted rather than assumed. A relative path, the parent directory, a UNC path, another launcher, `cmd.exe`, and the resolved path with `& calc.exe` / `" "extra-arg` / an embedded newline appended are all refused. A junction pointing at a lookalike is refused, and does not change what discovery chose. A confirmation captured before a `comfyui_path` change is refused |
| Is the copy into the install safe? | **Yes.** Separately consented (`confirm_copy_destination`), name through `validate_filename`, parent through `resolve_within_roots` **and** through `is_contained(..., user\default\workflows)` since S-22, exclusive create so it cannot overwrite, and six hostile destinations refused with the folder byte-identical. Consenting to the copy is not consent to start a program — asserted in both directions |
| Is the new SSE route bounded? | **Yes.** `/comfyui/launch/stream` calls `require_stream_capacity` before the headers go out, so it inherits the S-03 cap: 32 live subscribers, the 33rd a `503 FEATURE_UNAVAILABLE`, and the bus-level `SubscriberLimitError` behind it. It closes on `done`, and since S-23 `done` is published even when the wait fails, so a finished launch cannot hold a slot |
| Is it reachable from MCP? | **No, and now pinned.** No `launch`/`open`/`launcher` string anywhere in `registry.TOOLS`, no tool whose name contains either word, and `start_comfyui` is named in exactly one file in the whole application — `comfyui_service` itself, reached only through `open_workflow`, whose only caller is the REST route. The startup path does not import `comfyui_service` at all, so nothing scheduled or automatic can reach it |
| Is a launch disclosed and recorded? | **Disclosed, not recorded.** The status carries the resolved path, port, url, pid, trigger and both timestamps, and leaks no environment and no secret. Nothing is persisted — **S-24, open**, the same standing the updater has |
| Process hygiene | **Sound.** A new console is deliberate; ComfyUI outliving the vault is the design. No pipes are opened, so no handle is held for output that will never be read, and the `Popen` object is the only reference — on Windows there is no `_active` list to leak into. A launcher that exits first is reported with its exit code, a timeout says it timed out, and since S-23 a wait that raises still ends the launch instead of wedging it |

### 8.2 The detection pass — 2026-08-23 (defect fix, no finding)

Three defects were fixed in "Open in ComfyUI" after its first real use: detection missed a
running ComfyUI, the workflow did not load once it was open, and it opened a tab rather than a
window. The one change with a security surface is that the liveness probe now speaks HTTP
where it used to open a socket and drop it. Recorded here because a probe that speaks can be
pointed somewhere.

* **Reach.** The probe connects only to `127.0.0.1` and `::1`, and only to a port. The host is
  a literal in `LOOPBACK_PROBES`; the *port* can come from a launcher script on disk, is
  range-checked, and cannot influence the host. Asserted by
  `test_the_probe_only_ever_talks_to_the_loopback`.
* **Request shape.** Two fixed paths (`/system_stats`, the template list) plus the official
  template index. A template or source name is matched **inside** the answer, as a dict key —
  it is never interpolated into a request line, which is the obvious implementation of that
  function and the one that would have been traversable. Asserted by
  `test_the_probe_requests_fixed_paths_and_nothing_a_caller_chose` with hostile names.
* **No control moved.** `running` still means "a port is taken" and is still what the update
  refusal decides on — the new, stronger "it answered as ComfyUI" signal is reported
  *alongside* it, and is used only to decide that nothing needs starting. A port held by
  something that is not ComfyUI still blocks the updater
  (`test_a_port_that_is_taken_by_something_else_still_blocks_the_updater`). The verified-install
  gate (S-20), the launcher-evidence gate (S-21), `cmd_line_hazard` (S-19), the verbatim path
  confirmation and the copy containment (S-22) are untouched, and their tests are unchanged.
* **S-23 shape re-checked at the new entry point.** `is_running` now takes ports discovered
  from launcher scripts; a value that is not a port is discarded before any socket sees it,
  and the probe still cannot raise into a caller
  (`test_a_port_that_is_not_a_port_cannot_strand_the_probe`).
* **Spawn surface unchanged.** Two `Popen` call sites, and probing starts nothing and writes
  nothing (`test_probing_starts_nothing_and_writes_nothing`).

S-24 remains open and unchanged: a launch is still disclosed and not recorded.

---

## 9. Sign-off

**Signed off for the findings raised in this review.** Both Highs, all seven Mediums, the dependency floors and both QA defects are fixed and gated. Every test that was written as an `xfail` reproduction is now a hard regression gate carrying a comment that says what a failure there would mean — there is no `xfail` left in the suite.

Still open, all Low, all hardening with no demonstrated exploit and no reproduction that escapes a control:

| ID | Why it is still open |
|---|---|
| S-10 | `POST /system/roots` accepts any existing directory. A deliberate user action behind the CSRF header, exposed by no MCP tool |
| S-11 | `rmtree` gated on `isdir` rather than on `kind in DIRECTORY_KINDS` — a defence-in-depth tightening; no `models`/`outputs` row can point at a directory today |
| S-12 | `trash_empty`'s `rmtree` is not re-validated against `.vault-trash`; the value is only ever written by `_to_trash` |
| S-13 | `torch_zip` entry-count cap; a 20,000-entry archive parses in 152 ms and nothing is ever extracted |
| S-14 | `/system/validate-path` workflow walk is outside the 60,000-entry / 4-second budget |
| S-24 | A process launch is recorded in memory only. Adding a persisted record is a schema change and a UI surface, not a review-pass fix; what is asserted is the honest current state, including that no such table exists yet |

**Three things the next pass should know**, all discovered while fixing findings rather than during an audit:

1. **`Image.open(formats=[...])` is a footgun.** Pillow re-raises a bare `KeyError` for a format name the local build has not registered, turning every unrecognised file into a `KeyError` instead of an `UnidentifiedImageError`. Always filter the allowlist through `Image.OPEN` — `imaging.open_formats()` does.
2. **"List argv, `shell=False`" is not a Windows guarantee for a `.bat`.** `CreateProcess` runs a batch target through `cmd.exe`, which re-parses the whole command line including the executable's own path, and `list2cmdline` quotes a token only when it holds a space. Anything in this codebase that starts a `.bat`/`.cmd` must go through `comfyui_service.cmd_line_hazard()` first (S-19). The other three `Popen` sites resolve to real executables — `git.EXE` via `shutil.which`, `ffmpeg`, `explorer.exe` — so none of them is affected today; the next feature that starts a script would be.
3. **The updater and the launcher share one definition of a real install.** `comfyui_service.INSTALL_PROOF` / `missing_install_proof()`. They drifted apart once (S-04 fixed, S-20 not), which is exactly how the launcher ended up running a staged directory's own batch file; a test now pins that the router and the service use the same object.

`backend/tests` — **1,642 passed, 4 skipped, 0 xfailed** (56 of them the new `tests/security/test_comfyui_launcher.py`), `ruff check backend` clean, hermetic, safe to run against a machine holding the real library. Library verified unchanged after the pass: **a real-world library of several hundred models and thousands of outputs**, trash empty.

*Geekatplay Studio — Vladimir Chopine*
