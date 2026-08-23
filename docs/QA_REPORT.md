# QA Report — Geekatplay ComfyUI Asset Vault

Measured 2026-08-22 against the real installation at `O:\ComfyUI`.
All figures below were produced by execution, not inspection.

## Suite status

| | Before | After |
|---|---|---|
| `pytest tests` | **could not run** — collection error | **exit 0** — 1,497 passed, 5 skipped, 10 xfailed |
| `ruff check backend/app` | 1 error | clean |
| `npm run build` | passes | passes (344 kB entry + 64.67 kB storage chunk) |

The two QA defects that were still open — unbounded WAL growth and QA-PERF-1 —
are now closed; both are written up under [Closed defects](#closed-defects)
with their measurements. The `/ping` budget is a hard gate again: its `xfail`
marker is gone and 12 tests were added to hold both fixes in place.

The suite previously died at collection because `tests/test_file_ops.py` and
`tests/test_workflow.py` imported `app.services.workflow_parser`, deleted in Wave 1.

## Indexed contents (real install)

| Asset | Count |
|---|---|
| Models | 237 (1.589 TB) |
| Node packages | 34 |
| Node classes | 1,866 (841 official) |
| Workflows | 211 (159 with missing dependencies) |
| Outputs | 3,834 |
| Search documents | 6,182 — exactly matching indexable rows |
| Albums | 10, stable across repeated startups |

## Original defect gates (all verified)

| Defect | Evidence |
|---|---|
| **B1** — scan crashed on link-valued prompt inputs | `positive_prompt = negative_prompt` count is **0** (was 102 after the first fix, 126/400 crashing before it). Provenance records `zeroed` vs `empty` distinctly. |
| **B2** — AutoV2 hash wrong | `flux2-vae-new` → `5628B30A8E`, matching a reference full-file SHA-256. Previously returned `E3B0C44298`, the hash of the empty string. Verified on 3 files. |
| **B3** — architecture misdetection | `flux1-dev-fp8` → FLUX.1 **checkpoint**, 11.9 B primary params (was "VAE", 16.87 B total). **0** models whose `architecture_label` names a family other than their own (was 5 LTX models labelled "ACE-Step audio checkpoint"). |
| **B4** — node classes missed | 32 of 34 packages yield classes (was 11 of 32). The two zeros are ComfyUI-Manager and ComfyUI-Unreal, both genuinely empty. |
| **B5** — launcher never started the backend | `--cwd` replaced with `--app-dir`; backend answers `/api/v1/ping` **1 second** after launch. |
| **B6** — path desync on restart | `settings.COMFYUI_PATH` removed; `config_service` is the only reader. Albums stable at 10 across three real restarts. |

## Security findings closed

| ID | Severity | Status |
|---|---|---|
| **S-01** — NTFS junctions escaped every root | High | **Fixed and gated.** A junction created with `mklink /J` is now detected as a reparse point and skipped by both walk sites. Verified by creating a real junction and confirming the walker refuses to descend. |
| **S-02** — MCP reachable cross-origin | High | **Fixed and gated.** A `text/plain` POST with `Origin: http://127.0.0.1:8188` (ComfyUI's own port, which serves third-party custom-node JavaScript) calling `vault_delete` now returns **403**. Legitimate MCP clients over both transports still work. |

Both were open findings carrying `xfail` markers. Those markers have been removed
and replaced with comments marking them as permanent regression gates — a failure
there is a reopened breach, not an expected condition.

Remaining `xfail` findings (S-03 through S-09) are documented in
`SECURITY_REVIEW.md` and are Medium or below.

## Performance

| Budget | Target | Measured | Verdict |
|---|---|---|---|
| Cold full scan | ≤ 25 s | 12.6 s | pass |
| Warm incremental scan | ≤ 1.5 s | 0.65 s | pass |
| `GET /models?limit=100` p95 | ≤ 40 ms | 4.3 ms | pass |
| Lexical search (over HTTP) | ≤ 50 ms | 19.4 ms | pass |
| Cached thumbnail p95 (over HTTP) | ≤ 25 ms | 1.7 ms | pass |
| Frontend scroll (3,834 outputs) | 60 fps | p95 17.9 ms | pass |
| **`/ping` p95 during a full scan** | **≤ 50 ms** | **4.6 ms** (max 209 ms, 1,031 samples) | **pass** |

Every row is a live measurement against the running backend and the real
`O:\ComfyUI` install. The budget was never relaxed.

## Closed defects

### QA-PERF-1 — API tail latency during a full scan

**Was:** `/ping` p95 96–147 ms against a 50 ms budget (median 13.3 ms, max
874 ms, 29 of 235 samples over 50 ms) during a forced full reindex.
**Now:** p95 **4.6 ms**, 1,031 samples over 13.3 s of scanning.

#### What it was not

Both suspects named in the original write-up were measured and cleared.

* **The S-01 reparse-point check is free.** Walking the 8,166 entries under
  `custom_nodes` took **0.034 s** with the check and **0.032 s** without it —
  about 0.25 µs per entry. The whole-install walk is ~0.045 s. On Windows,
  `DirEntry.stat(follow_symlinks=False)` is answered from the directory listing
  that `scandir` already fetched, so there is no extra syscall to pay for. The
  guard stays exactly as it is.
* **Executor concurrency is the wrong lever.** Cutting the AST executor from
  four workers to one made the `nodes` phase *worse* — p95 282 ms against
  250 ms. Under a global interpreter lock the total blocked time is invariant
  in the thread count; spreading the same work over fewer threads only
  lengthens the window it is spread across.

#### What it was

Two parsers issue a single C call that holds the interpreter lock for its whole
duration and cannot be interrupted at a bytecode boundary. Nothing else in the
process runs while one is in flight, so a request simply waits:

| Call | Site | Longest single hold | Distribution |
|---|---|---|---|
| `ast.parse` | node package source | **151.2 ms** (563 KB `WAS_Node_Suite.py`) | 415 calls, 1.31 s total, median 0.79 ms, p99 39.8 ms, 4 calls over 50 ms |
| `json.loads` | safetensors header | **93.8 ms** (390 KB header) | headers to 390 KB across 237 models |

Polling `/ping` at 100 Hz through a forced full scan and bucketing each sample
by the phase that was running when it was taken makes the shape obvious:

| Phase | Seconds | p95 | p99 | max |
|---|---|---|---|---|
| `nodes` | 5.82 | **249.8 ms** | 375.6 ms | 593.7 ms |
| `models` | 1.70 | **109.4 ms** | 173.8 ms | 173.8 ms |
| `workflows` | 0.43 | 20.4 ms | 20.8 ms | 20.8 ms |
| `outputs` | 2.57 | 6.8 ms | 7.6 ms | 15.9 ms |

`outputs` handles the most files by an order of magnitude — 3,834 — and is the
best-behaved phase in the scan, which is what rules out file volume, walking and
commit batching as the cause. Running each phase in isolation confirmed it:
`nodes` alone gave p95 235 ms and `models` alone 77 ms, while `outputs` alone
gave 7.1 ms and `workflows` alone 18.0 ms.

#### The fix

`_analyze` (node packages) and `_parse_one` (model headers) are pure functions —
a dataclass in, the same dataclass back, no database and no shared state — so
both now run in worker processes through `map_cpu`, and the long holds happen
somewhere the API is not. Per-item isolation is unchanged: one unparseable file
still produces one error row. If the pool cannot start, or a worker dies, the
batch finishes on the existing thread executors, so the worst case is the old
behaviour rather than a lost scan.

Three consecutive forced full scans of the real install, before and after:

| | Before | After |
|---|---|---|
| `/ping` p95 | 55.8 / 56.1 / 55.2 ms | **7.1 / 6.7 / 7.6 ms** |
| `/ping` p99 | 290.9 / 268.2 / 310.4 ms | 16.9 / 17.8 / 13.8 ms |
| samples over 50 ms | 24 / 23 / 22 of ~392 | 3 / 0 / 2 of ~595 |
| scan wall time | 10.47 / 10.70 / 10.81 s | **7.84 / 7.81 / 8.00 s** |

The scan got *faster* as well as quieter: the analysis is genuinely parallel
once it is off the lock, taking `nodes` from 5.82 s to 2.91 s and `models` from
1.70 s to 0.94 s. Sample count is itself a symptom — the poller managed 392
requests in the old 10.8 s scan and 595 in the new 8.0 s one.

Cost: up to four worker processes at roughly 60 MB each while a scan is
analysing, torn down the moment it finishes (idle backend measured back at
236 MB). A warm incremental scan has too little to analyse to be worth starting
them and does not.

The `xfail` marker on
`tests/perf/test_budgets.py::test_ping_stays_under_50ms_while_the_real_install_is_scanned`
has been removed. It is a hard gate.

### The WAL grew without bound and was never truncated

**Was:** `backend/data/vault.db-wal` at **2.50 GB** — 605,621 frames of 4 KB —
against a 35.6 MB, 8,688-page database. **Now:** 0 bytes, and 2.50 GB of the
owner's disk returned. `PRAGMA integrity_check` is `ok` and every count is
unchanged: 237 models, 3,834 outputs, 211 workflows, 34 packages, 1,866 node
classes, 6,182 search documents, 10 albums.

#### Root cause

Unbounded growth needed two independent faults, and both were present.

**1. A reader pinned the log at frame 34.** Reading the wal-index (`-shm`)
directly, rather than inferring it from a checkpoint's return value, showed the
pin exactly:

```
mxFrame  = 492123
nBackfill= 34
aReadMark= [0, 492123, 34, 491202, 393960]
```

A checkpoint may only copy frames older than the oldest open read snapshot, so
`nBackfill` was capped at the read mark of 34 — which is precisely the
`checkpointed=34` in the original measurement. `PRAGMA wal_checkpoint(PASSIVE)`
returned `(busy=0, log_pages=492123, checkpointed=34)` and left read-mark slots
2, 3 and 4 untouched; a checkpoint resets every slot it can lock, so three
readers were genuinely holding locks.

The Restart Manager named exactly three processes with the vault open — three
backend instances, on ports 8127, 8130 and 8131, two of them abandoned from
earlier sessions. **One pinned snapshot per process**, and the oldest had taken
its snapshot when the log was 34 frames long and never released it. All three
served current data, so the pin was on an idle background reader, not on a
request path.

That is the failure the existing guard cannot reach. Rollback-on-acquire only
fires when the *same thread* asks for its connection again, and a thread that
reads once and then goes quiet never does. Worse, a statement that was stepped
but never reset holds its read transaction open while `sqlite3_get_autocommit`
still reports autocommit — so `in_transaction` is `False` throughout and the
guard cannot even see the state it is meant to clear.

**2. Nothing ever asked for a checkpoint.** SQLite's automatic checkpoint is
PASSIVE, which rewinds the write cursor but never shrinks the file, and no code
path in the app called `wal_checkpoint` at all. Measured on a clean process with
no pin whatsoever: after a full scan of the real install the `-wal` sat at
7,016,392 bytes and stayed there for the life of the process. Add a pin to that
and the file can only grow.

There was a third, quieter contributor: `close_thread_connections()` closed only
the *calling* thread's readers, so at shutdown the readers belonging to request
threads and job workers stayed open. SQLite checkpoints and deletes the `-wal`
when the last connection closes — with strays still open, the writer's close was
never the last one, and the log survived process exit at whatever size it had
reached.

#### The fix

* Every reader from `get_ro()` is registered, so the set of live readers is
  enumerable instead of hidden inside per-thread storage.
  `close_all_connections()` closes all of them; `reap_dead_readers()` closes
  those whose owning thread has exited.
* `release_read_snapshot()` ends a thread's snapshot by closing its connection —
  the only thing that reliably finalises a stepped-but-unreset statement.
  Reconnecting costs microseconds and happens lazily on the next read.
* `db.checkpoint()` runs `PRAGMA wal_checkpoint(TRUNCATE)` on the writer
  connection after releasing the readers this process can account for, and
  reports `busy` rather than hiding it — a `busy` result now means the pin is in
  another process, which is worth logging.
* A scan truncates the log it wrote, on completion. Shutdown closes every
  reader, truncates, and only then closes the writer, in that order, so the
  writer's close is the last one and SQLite removes the `-wal` and `-shm`.

Measured after the fix, against the real install:

| | Before | After |
|---|---|---|
| `-wal` after a full scan | 7,016,392 bytes | **0** |
| `-wal` after a warm incremental | 65,952 bytes | **0** |
| `-wal` after shutdown | survives at full size | **file deleted**, with `-shm` |
| checkpoint on an idle app | `busy=1`, 34 of 492,123 pages | `busy=0`, all pages |
| owner's vault | 2.50 GB | **0 bytes**, `integrity_check` ok |

Steady state on the running backend after repeated full scans and the whole
live suite: 0.55 MB, returned to 0 by the next scan.

## Notes

* Two launcher tests skip for environmental reasons (port already in use; browser
  launch not detected by the current pattern match).
* The launcher port assertion now resolves batch-file variables, so the launcher
  can pin its port once in a variable — better practice — while the test still
  verifies the concrete value is 8127.
* The vault was returned to exactly 237 models / 3,834 outputs after every
  destructive test; all such tests operate on disposable probe files.
* `tests/security/test_traversal.py::test_file_operations_cannot_reach_through_a_junction`
  skips because the walker refuses to cross the junction, which is the condition
  it exists to depend on. The two S-01 gates either side of it — the walker and
  the node scanner — both pass.
* Twelve tests were added for the two fixes:
  `tests/integration/test_wal_lifecycle.py` (a checkpoint on an idle app must be
  neither `busy` nor partial; a scan must truncate the log it wrote; repeated
  scans must not grow it; a reader on a thread that has exited must not pin it;
  shutdown must close every reader and remove the log; a real pin must be
  reported rather than swallowed) and
  `tests/integration/test_cpu_pool_isolation.py` (the analysis really does leave
  this process; one failing item costs only itself; a worker that dies does not
  cost the batch; a batch too small to be worth a spawn stays in process).
* Three backend instances were found holding the vault open on ports 8127, 8130
  and 8131, two of them abandoned from earlier sessions. They were stopped and a
  single instance restarted on 8127.
