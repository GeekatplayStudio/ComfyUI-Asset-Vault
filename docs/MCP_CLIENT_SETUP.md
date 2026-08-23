# Connecting an MCP client

Geekatplay ComfyUI Asset Vault · **Geekatplay — Vladimir Chopine**

The vault ships a Model Context Protocol server so an MCP-capable assistant can answer questions
about your ComfyUI library — and, if you let it, reorganise it. **26 tools**, two transports,
protocol version `2025-06-18`.

Everything here was executed against the running app before it was written down.

---

## Which transport

| | **stdio** | **Streamable HTTP** |
|---|---|---|
| Endpoint | `python -m app.mcp_stdio` | `http://127.0.0.1:8127/api/v1/mcp` |
| Needs the app running | no — it opens the database directly | yes |
| Client support | universal | good, and growing |
| Headers to configure | none | three, see below |
| Use it when | a desktop MCP client spawns servers as child processes | your client speaks Streamable HTTP, or you already have the app open |

Both share the same tool registry, the same database and the same safety rails. Only the framing
differs.

---

## stdio

Add this to your MCP client's server configuration. Adjust the paths to where you installed the
vault; Windows paths need doubled backslashes inside JSON.

```json
{
  "mcpServers": {
    "geekatplay-vault": {
      "command": "D:\\Projects\\ComfyUIAssetManager\\venv\\Scripts\\python.exe",
      "args": ["-m", "app.mcp_stdio"],
      "cwd": "D:\\Projects\\ComfyUIAssetManager\\backend",
      "env": {
        "VAULT_DB": "D:\\Projects\\ComfyUIAssetManager\\backend\\data\\vault.db"
      }
    }
  }
}
```

Three things matter and are easy to get wrong:

* **`cwd` must be the `backend` folder.** That is what makes `app.mcp_stdio` importable.
* **`command` must be the venv's `python.exe`**, not a system Python — the dependencies live in
  the virtual environment.
* **`VAULT_DB` is optional** but recommended; without it the server resolves the default path
  relative to its own location.

The process is deliberately thin: it does not start uvicorn, the indexer, or any background
executor. It writes **nothing but JSON-RPC to stdout** — all logging goes to stderr — which is the
single most common way a stdio MCP server breaks. Set `VAULT_LOG_LEVEL=DEBUG` in `env` if you want
to see what it is doing on stderr.

### Read-only mode

```json
"args": ["-m", "app.mcp_stdio", "--read-only"]
```

Every mutating tool then refuses with a clear message and changes nothing:

> `'vault_delete' modifies the vault and this server is running in read-only mode
> (mcp_read_only=true / --read-only). Nothing was changed.`

The same switch exists app-wide as the `mcp_read_only` config key, which also governs the HTTP
transport. **The shipped default is full access**, deliberately — see "The deal you are making"
below.

### Checking it works, without a client

```bat
cd backend
set VAULT_DB=D:\Projects\ComfyUIAssetManager\backend\data\vault.db
..\venv\Scripts\python.exe -m app.mcp_stdio
```

Paste these, one per line:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"mcp-client","version":"1.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
```

`tools/list` returns 26 tools. `Ctrl+C` to exit.

---

## Streamable HTTP

```
POST http://127.0.0.1:8127/api/v1/mcp
```

Every request needs **three** headers beyond the JSON-RPC body:

| Header | Value |
|---|---|
| `Content-Type` | `application/json` |
| `Accept` | `application/json, text/event-stream` |
| `X-Vault-Request` | `1` |

and after `initialize`, also `Mcp-Session-Id` (returned in the `initialize` response) and
`MCP-Protocol-Version: 2025-06-18`.

If your client sends an `Origin` header it must be **the vault's own origin** — one of
`http://127.0.0.1:8127`, `http://localhost:8127`, `http://[::1]:8127`. Any other origin is
refused with `403`, *including another port on the same loopback address*:

```json
{"error":{"code":"PATH_NOT_ALLOWED",
  "message":"This MCP endpoint only accepts requests from the vault's own origin. Another loopback port is not the same origin.",
  "details":{"origin":"http://127.0.0.1:8188","allowed":["http://127.0.0.1:8127","http://[::1]:8127","http://localhost:8127"]}}}
```

That is not pedantry. ComfyUI serves third-party JavaScript from custom node packages on its own
port; without this check, a hostile node could call `vault_delete` from your browser. A client
that sends no `Origin` at all — which most non-browser clients do not — is fine.

Missing `X-Vault-Request` gets a `403` that tells you exactly what to do:

```json
{"error":{"code":"CSRF_HEADER_MISSING",
  "message":"This MCP endpoint requires the header 'X-Vault-Request: 1' on every request. Add it to your MCP client's HTTP headers, or use the stdio transport (python -m app.mcp_stdio), which needs no headers."}}
```

The three verbs: `POST` for JSON-RPC, `GET` (with `Accept: text/event-stream`) for the
server-to-client notification stream, `DELETE` to end the session. Sessions expire after 30
minutes idle; an unknown session gets `404` with JSON-RPC error `-32001`.

### A working handshake

```bat
curl -s -X POST http://127.0.0.1:8127/api/v1/mcp ^
  -H "Content-Type: application/json" ^
  -H "Accept: application/json, text/event-stream" ^
  -H "X-Vault-Request: 1" ^
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-06-18\",\"capabilities\":{},\"clientInfo\":{\"name\":\"mcp-client\",\"version\":\"1.0\"}}}"
```

The response carries `Mcp-Session-Id` as a header and, in the body, the server's `instructions` —
which tell the assistant to prefer trash over permanent deletion and never to delete more than it
was asked to.

### Over the network

Don't. The engine binds loopback only. If you set `ALLOW_LAN=1` deliberately, the MCP router
additionally demands `Authorization: Bearer <token>`, and refuses to mount at all without one.

---

## The 26 tools

**Read — the library**

| Tool | |
|---|---|
| `vault_search` | Open-ended search across all five asset kinds. Start here for vague questions. |
| `list_models` · `get_model` | Filter, sort and page models; full technical detail for one. |
| `list_node_packages` · `list_node_classes` · `get_node_class` | Installed packages and every class they register. |
| `list_workflows` · `inspect_workflow` | Workflows, and one workflow's full dependency report. |
| `find_model_usage` | Which workflows and outputs use a given model. |
| `query_outputs` | Generated images and videos with their generation parameters. |
| `vault_stats` | Scale in one call. Call it first. |
| `get_index_status` | Whether a scan is running, and what the last one found. |

**Write — jobs**

| Tool | |
|---|---|
| `vault_reindex` | Re-scan. Streams progress notifications. |
| `vault_hash_enqueue` · `vault_hash_cancel` | Start and stop background hashing, scoped. |
| `vault_embeddings_rebuild` | Rebuild the semantic index. |

**Write — files**

| Tool | |
|---|---|
| `vault_rename` | One item. Can keep the extension and carry sidecars along. |
| `vault_move` | Up to 200 items to a root and folder, with a conflict policy. |
| `vault_delete` | Trash by default; `mode:"permanent"` demands `confirm:true`. |
| `vault_trash_list` · `vault_trash_restore` · `vault_trash_empty` | The safety net. `trash_empty` always requires `confirm:true`. |
| `vault_create_folder` | |
| `vault_assign_tags` | Metadata only; touches no file. |

**Write — making a workflow runnable**

| Tool | |
|---|---|
| `enable_workflow_plan` | The dependency report for one workflow: every missing model with the folder it belongs in, every missing package with its registry repository, the download size, and free space per target drive. **Downloads nothing.** Returns a short-lived `plan_token`. |
| `enable_workflow_fetch` | Fetches only the `item_ids` the user picked from that report. Needs the `plan_token`, the ids, and `confirm:true` — there is no fetch-everything shorthand, and a stale plan is refused. Models only from Civitai or Hugging Face, node packages only from registry-declared repositories, every redirect re-checked. Packages are **cloned, never installed** — no `pip install`, no `requirements.txt`, no `install.py`. |

The pattern is the point: an agent must show the plan and get a choice before anything downloads.
`on_conflict` accepts `fail`, `skip` or `keep_both`; `overwrite` does not exist on this path.

### Resources and prompts

Five resources — `vault://stats`, `vault://models/index`, `vault://nodes/index`,
`vault://workflows/index`, `vault://health` — plus templates for one model, workflow, workflow
graph or node class by id.

Four prompts: `diagnose_workflow`, `recommend_model`, `vault_overview`, `find_unused`.

---

## The deal you are making

**Full file-operation access is the shipped default.** That was a deliberate choice by the project
owner, made with the risk stated plainly: a hallucinated tool call can destroy models that take
hours to re-download. It was not narrowed back to read-only behind your back.

What stands between a bad call and a lost checkpoint:

1. **Trash is the default.** `mode` defaults to `trash`. Deleted files go to `.vault-trash` inside
   the root they came from and come back with `vault_trash_restore`. Exactly what the interface
   does when you click Delete.

2. **Permanent deletion needs `confirm:true`.** Without it, nothing happens:

   > `Permanent deletion requires confirm=true. Nothing was deleted. Use mode='trash' (the
   > default) to keep the files recoverable.`

3. **200 items per call, maximum.** Beyond that the tool errors and tells the agent to page. One
   bad call cannot touch the whole library.

4. **Every mutation is audited, and you can read the audit.** An `mcp_audit` row records the
   timestamp, session, transport, tool, **the full arguments**, the items affected, the outcome and
   how long it took. Read tools deliberately do not log argument values — prompts can be sensitive
   — but mutations always do. Open **Settings → Activity** to read the log, or call
   `GET /api/v1/mcp/audit`. Both are read-only: nothing can edit or delete an entry.

5. **Roots are enforced identically.** Every mutation goes through the same path guard and the
   same file-operation code the interface uses. MCP gets no privileged path.

6. **No tool accepts a filesystem path.** Input is by `uid` only. Absolute paths appear in
   *output*, and only for assets already indexed.

7. **No file content is ever served.** There is no read-file tool. A checkpoint cannot be
   exfiltrated through MCP.

8. **No network egress on the agent's behalf.** Tools report cached state; they never trigger a
   Civitai, GitHub or Ollama call. The vault cannot be used as a request pivot.

9. **Rate limited** to 120 tool calls per minute per session, returned as a retryable result
   rather than a protocol error so the agent backs off gracefully.

If any of that is more latitude than you want, run with `--read-only` (stdio) or set
`mcp_read_only=true` (both transports). Nothing else changes; the read tools all still work.

---

## When it does not connect

| Symptom | Cause |
|---|---|
| Client reports the server exited immediately | Wrong `cwd`. It must be `backend\`, or `app.mcp_stdio` is not importable. |
| `No module named 'fastapi'` on stderr | `command` points at a system Python, not `venv\Scripts\python.exe`. |
| Garbage or parse errors on the wire | Something wrote to stdout. Check stderr — the server routes all logging there deliberately, so this points at a local modification. |
| HTTP `403 CSRF_HEADER_MISSING` | Add `X-Vault-Request: 1`, or switch to stdio, which needs no headers. |
| HTTP `403 PATH_NOT_ALLOWED` | Your client sends an `Origin` that is not the vault's own. Remove it, or set it to `http://127.0.0.1:8127`. |
| HTTP `404` with `-32001` | The session expired (30 minutes idle). Re-`initialize`. |
| Every mutating tool refuses | Read-only mode is on — the `--read-only` flag or `mcp_read_only=true`. |
| `tools/list` returns fewer than 26 tools | You are talking to something else, or to an older build. |

For anything else, `backend_log.txt` (HTTP transport) or the server's stderr (stdio) has it.
