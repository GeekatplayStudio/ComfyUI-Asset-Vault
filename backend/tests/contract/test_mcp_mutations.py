"""DECISIONS C5.2: the file-operation tools, driven end to end over MCP.

**The owner's library must end this run exactly as it started.**  That is the
whole design constraint of this module, and it shapes every choice in it:

* every mutation is aimed at a *disposable probe* -- a tiny workflow JSON this
  module writes into the real ComfyUI tree itself and takes away again through
  the shared ``probes`` fixture, whose teardown asserts the removal worked;
* no real model, workflow or output is ever renamed, moved, tagged, trashed or
  deleted.  ``vault_hash_enqueue`` is therefore never pointed at a real model
  either: hashing a real file would write a hash the vault did not have before,
  so only the empty-scope and refusal paths are exercised here;
* ``vault_trash_empty`` is only ever called with an explicit ``ids`` list of a
  trash entry this module created, or with an ``older_than_days`` window wide
  enough that nothing real can fall inside it;
* the probe tag is deleted from the ``tags`` table afterwards, so not even an
  unused row is left behind;
* a final rescan proves no probe row survives in the index.

The refusal paths matter as much as the happy path: a batch over the cap, a
permanent delete without ``confirm``, and an empty-trash without ``confirm``
must all fail *before* touching the disk, and must still be audited.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.contract

MCP_PATH = "/api/v1/mcp"
PROTOCOL_VERSION = "2025-06-18"
JSON_HEADERS = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "X-Vault-Request": "1"}
CLIENT_NAME = "mutation-probe"
#: The REST CSRF header, needed for the one REST cleanup call this module makes.
VAULT_REQUEST = {"X-Vault-Request": "1"}

#: Everything this module creates starts with this, so a leftover is obvious in
#: a file listing and easy to sweep up.
PROBE_PREFIX = "zz_mcp_probe_"
#: Wide enough that no entry in a real trash can be inside the window.
ANCIENT_DAYS = 3650
SCAN_TIMEOUT_S = 300

#: A minimal but structurally real ComfyUI graph: two nodes, one link.  Small
#: enough to index in milliseconds, real enough to be a workflow row.
PROBE_GRAPH = {
    "last_node_id": 2, "last_link_id": 1,
    "nodes": [
        {"id": 1, "type": "EmptyLatentImage", "pos": [0, 0], "size": [270, 106],
         "flags": {}, "order": 0, "mode": 0, "inputs": [],
         "outputs": [{"name": "LATENT", "type": "LATENT", "links": [1]}],
         "properties": {"Node name for S&R": "EmptyLatentImage"},
         "widgets_values": [512, 512, 1]},
        {"id": 2, "type": "PreviewImage", "pos": [400, 0], "size": [210, 26],
         "flags": {}, "order": 1, "mode": 0,
         "inputs": [{"name": "images", "type": "IMAGE", "link": 1}],
         "outputs": [], "properties": {"Node name for S&R": "PreviewImage"}},
    ],
    "links": [[1, 1, 0, 2, 0, "IMAGE"]],
    "groups": [], "config": {}, "extra": {}, "version": 0.4,
}


# =============================================================================
# In-process: the safety rails are declared in the schemas themselves
# =============================================================================

def test_the_batch_cap_is_two_hundred():
    from app.mcp import registry

    assert registry.MAX_BATCH == 200


@pytest.mark.parametrize("tool_name", ["vault_move", "vault_delete",
                                       "vault_assign_tags"])
def test_batch_tools_declare_the_cap_in_their_schema(tool_name):
    """An agent must be able to *read* the cap, not discover it by being
    refused."""
    from app.mcp import registry

    uids = registry.BY_NAME[tool_name].input_schema["properties"]["uids"]
    assert uids["maxItems"] == registry.MAX_BATCH


def test_irreversible_tools_take_a_confirm_argument():
    from app.mcp import registry

    assert "confirm" in registry.BY_NAME["vault_delete"].input_schema["properties"]
    trash_empty = registry.BY_NAME["vault_trash_empty"].input_schema
    assert "confirm" in trash_empty["properties"]
    assert "confirm" in trash_empty["required"]


def test_trash_is_the_default_delete_mode():
    """Nothing may be destroyed because an agent left an argument out."""
    from app.mcp import registry

    mode = registry.BY_NAME["vault_delete"].input_schema["properties"]["mode"]
    assert mode["default"] == "trash"


def test_every_file_operation_tool_is_audited():
    from app.mcp import registry

    file_ops = ("vault_rename", "vault_move", "vault_delete", "vault_trash_restore",
                "vault_trash_empty", "vault_create_folder", "vault_assign_tags")
    assert all(registry.BY_NAME[name].audited for name in file_ops)
    assert all(registry.BY_NAME[name].mutating for name in file_ops)


# =============================================================================
# Live plumbing
# =============================================================================

class McpHttp:
    """A minimal MCP client over the streamable-HTTP transport."""

    def __init__(self, base_url: str, *, timeout: float = 300.0) -> None:
        self.url = base_url.rstrip("/") + MCP_PATH
        self.client = httpx.Client(timeout=timeout)
        self.session_id: str | None = None
        self._next_id = 0

    def _post(self, body: dict, *, with_session: bool = True) -> httpx.Response:
        headers = dict(JSON_HEADERS)
        if with_session and self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return self.client.post(self.url, headers=headers,
                                content=json.dumps(body).encode("utf-8"))

    def open(self) -> McpHttp:
        self._next_id += 1
        response = self._post(
            {"jsonrpc": "2.0", "id": self._next_id, "method": "initialize",
             "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                        "clientInfo": {"name": CLIENT_NAME, "version": "1.0"}}},
            with_session=False)
        self.session_id = response.headers["mcp-session-id"]
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def call(self, name: str, arguments: dict | None = None) -> dict:
        """``tools/call``; a tool-level failure comes back as a result."""
        self._next_id += 1
        body = self._post({"jsonrpc": "2.0", "id": self._next_id,
                           "method": "tools/call",
                           "params": {"name": name,
                                      "arguments": arguments or {}}}).json()
        assert "error" not in body, body["error"]
        return body["result"]

    def call_ok(self, name: str, arguments: dict | None = None) -> dict:
        result = self.call(name, arguments)
        assert result.get("isError") is not True, result["content"][0]["text"]
        return result["structuredContent"]

    def close(self) -> None:
        self.client.close()


def error_text(result: dict) -> str:
    assert result.get("isError") is True, "the call was expected to be refused"
    return result["content"][0]["text"]


def assert_keys_declared(tool_name: str, payload: dict) -> None:
    """Every key a mutation returns must be in its published ``outputSchema``.

    The schemas declare ``additionalProperties: false``, so an undeclared key is
    a contract break even though JSON-RPC would happily carry it.
    """
    from app.mcp import registry

    declared = set(registry.BY_NAME[tool_name].output_schema["properties"])
    assert not set(payload) - declared, \
        f"{tool_name}: undeclared key(s) {sorted(set(payload) - declared)}"


@pytest.fixture(scope="module")
def mcp(running_server):
    client = McpHttp(running_server).open()
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="module")
def rest(running_server):
    with httpx.Client(base_url=running_server.rstrip("/") + "/api/v1",
                      timeout=120.0) as client:
        yield client


@pytest.fixture(scope="module")
def comfy_root() -> tuple[int, Path]:
    """The default ComfyUI root, as the running server has it indexed."""
    from app.config import DB_PATH

    if not Path(DB_PATH).exists():
        pytest.skip("no indexed vault.db")
    conn = sqlite3.connect(f"file:{Path(DB_PATH).as_posix()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT id, path FROM roots WHERE kind = 'comfyui' "
            "ORDER BY is_default DESC, id LIMIT 1").fetchone()
    finally:
        conn.close()
    if row is None:
        pytest.skip("no ComfyUI root registered")
    root = Path(row[1])
    if not (root / "workflows").is_dir():
        pytest.skip(f"no workflows folder under {root}")
    return int(row[0]), root


def wait_for_idle(rest: httpx.Client) -> None:
    deadline = time.time() + SCAN_TIMEOUT_S
    while rest.get("/index/status").json().get("active"):
        assert time.time() < deadline, "a scan was still running after 5 minutes"
        time.sleep(0.25)


def rescan(mcp: McpHttp, rest: httpx.Client) -> None:
    """One incremental workflow pass, waited out by job id.

    ``vault_reindex`` answers as soon as the job is *queued*, so polling for
    ``active == false`` can return before the scan has even started and read a
    vault that has not seen the probe yet.  The job id the tool hands back is
    the only reliable finish line.  Only the workflow phases are requested: a
    full pass re-reads every model and every output, which a probe workflow
    does not need and which turns a 0.1 s wait into a 10 s one.
    """
    wait_for_idle(rest)
    started = mcp.call_ok("vault_reindex", {"mode": "incremental",
                                            "phases": ["workflows"]})
    assert started["started"] is True, started
    job_id = int(started["job_id"])
    deadline = time.time() + SCAN_TIMEOUT_S
    while True:
        last = (rest.get("/index/status").json().get("last_completed") or {}).get("id")
        if last is not None and int(last) >= job_id:
            return
        assert time.time() < deadline, f"scan job {job_id} never finished"
        time.sleep(0.2)


#: The tables whose row counts must be identical before and after this module.
CENSUS_TABLES = ("models", "model_files", "workflows", "outputs", "node_packages",
                 "node_classes", "albums", "tags", "asset_tags", "trash_items")


def vault_census() -> dict[str, int]:
    """Row counts for everything a mutation test could plausibly disturb."""
    from app.config import DB_PATH

    conn = sqlite3.connect(f"file:{Path(DB_PATH).as_posix()}?mode=ro", uri=True)
    try:
        return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
                for table in CENSUS_TABLES}
    finally:
        conn.close()


def probe_row_uids(needle: str) -> list[str]:
    """Every workflow row whose path carries ``needle``, soft-deleted included.

    ``list_workflows`` hides a row whose file has vanished (``missing_since``
    is set and the row lingers for the 30-day retention), so the table itself
    is the only place that can prove nothing was left behind.
    """
    from app.config import DB_PATH

    assert needle, "refusing to match every workflow row"
    conn = sqlite3.connect(f"file:{Path(DB_PATH).as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id FROM workflows WHERE name LIKE ? OR rel_path LIKE ?",
            (f"%{needle}%", f"%{needle}%")).fetchall()
    finally:
        conn.close()
    return [f"workflow:{int(r[0])}" for r in rows]


def purge_probe_rows(mcp: McpHttp, needle: str) -> None:
    """Take the probe out of the index as well as off the disk.

    Deleting the file alone would leave a soft-deleted row behind at the next
    scan, and a row the owner never created is a change to the vault.
    """
    uids = probe_row_uids(needle)
    if uids:
        mcp.call("vault_delete", {"uids": uids, "mode": "permanent",
                                  "confirm": True})


def audit_rows(session_id: str) -> list[dict]:
    """Every audit row this MCP session wrote, oldest first.

    Filtering by session id rather than by row id keeps the assertions honest
    even if something else is talking to the same server at the same time.
    """
    from app.config import DB_PATH

    conn = sqlite3.connect(f"file:{Path(DB_PATH).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM mcp_audit WHERE session_id = ? ORDER BY id",
            (session_id,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def tags_of(uid: str) -> list[str]:
    """The tag names attached to one asset, read straight from the vault."""
    from app.config import DB_PATH

    conn = sqlite3.connect(f"file:{Path(DB_PATH).as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT t.name FROM asset_tags a JOIN tags t ON t.id = a.tag_id "
            "WHERE a.uid = ? ORDER BY t.name", (str(uid),)).fetchall()
    finally:
        conn.close()
    return [str(r[0]) for r in rows]


def drop_tag(rest: httpx.Client, name: str) -> None:
    """Take an unused probe tag row back out of the vault."""
    for item in rest.get("/tags", params={"limit": 200}).json()["items"]:
        if item["name"] == name:
            assert rest.delete(f"/tags/{item['id']}",
                               headers=VAULT_REQUEST).status_code == 200


def track(probes, path: str | Path) -> Path:
    """Hand a path the *server* created to the fixture that removes it."""
    resolved = Path(path)
    probes.paths.append(resolved)
    return resolved


class Probe:
    """One disposable workflow, indexed and addressable by uid."""

    def __init__(self, stem: str, uid: str, path: Path, root_id: int, root: Path):
        self.stem = stem
        self.uid = uid
        self.path = path
        self.root_id = root_id
        self.root = root


@pytest.fixture(scope="module")
def probe_sweeper(mcp, rest):
    """The gate that makes leakage impossible to miss.

    Counts every table a mutation could disturb on the way in, sweeps up on the
    way out, and refuses to pass unless the vault holds exactly the rows it
    started with.  Deleting a probe *file* is not enough: the indexer soft
    deletes the row and keeps it for the 30-day retention, so the row has to go
    through ``vault_delete`` as well.
    """
    before = vault_census()
    try:
        yield
    finally:
        purge_probe_rows(mcp, PROBE_PREFIX)
    listed = mcp.call_ok("list_workflows",
                         {"name_contains": PROBE_PREFIX, "limit": 50})
    assert listed["total"] == 0, \
        f"probe workflow(s) still indexed: {listed['workflows']}"
    assert not probe_row_uids(PROBE_PREFIX), "a probe row survived the run"
    after = vault_census()
    drift = {table: (before[table], after[table])
             for table in CENSUS_TABLES if before[table] != after[table]}
    assert not drift, f"this module changed the vault: {drift}"


@pytest.fixture
def probe(probes, mcp, rest, comfy_root, probe_sweeper) -> Probe:
    """A throwaway workflow written into the real tree, scanned, and removed."""
    root_id, root = comfy_root
    stem = PROBE_PREFIX + uuid.uuid4().hex[:8]
    path = probes.file(root / "workflows" / f"{stem}.json",
                       json.dumps(PROBE_GRAPH).encode("utf-8"))
    rescan(mcp, rest)
    listed = mcp.call_ok("list_workflows", {"name_contains": stem, "limit": 5})
    rows = [w for w in listed["workflows"] if w["name"] == stem]
    assert rows, f"the probe workflow was not indexed: {listed['workflows']}"
    try:
        yield Probe(stem=stem, uid=rows[0]["uid"], path=path, root_id=root_id,
                    root=root)
    finally:
        purge_probe_rows(mcp, stem)


@pytest.fixture
def probe_folder(probes, mcp, comfy_root) -> str:
    """A throwaway folder, created through ``vault_create_folder`` itself."""
    root_id, _root = comfy_root
    rel = f"workflows/{PROBE_PREFIX}dir_{uuid.uuid4().hex[:8]}"
    created = mcp.call_ok("vault_create_folder", {"root_id": root_id, "folder": rel})
    track(probes, created["abs_path"])
    return rel


# =============================================================================
# The probe itself
# =============================================================================

@pytest.mark.live
@pytest.mark.mcp
def test_the_probe_is_indexed_and_addressable(probe, mcp):
    assert probe.path.is_file()
    assert probe.uid.startswith("workflow:")
    inspected = mcp.call_ok("inspect_workflow", {"uid": probe.uid})
    assert inspected["workflow"]["name"] == probe.stem
    assert inspected["workflow"]["node_count"] == len(PROBE_GRAPH["nodes"])


# =============================================================================
# vault_create_folder
# =============================================================================

@pytest.mark.live
@pytest.mark.mcp
def test_vault_create_folder_creates_a_real_directory(probes, mcp, comfy_root):
    root_id, root = comfy_root
    rel = f"workflows/{PROBE_PREFIX}dir_{uuid.uuid4().hex[:8]}"
    created = mcp.call_ok("vault_create_folder", {"root_id": root_id, "folder": rel})
    assert_keys_declared("vault_create_folder", created)
    made = track(probes, created["abs_path"])
    assert made.is_dir()
    assert made.as_posix().lower().endswith(rel.lower())
    assert made.is_relative_to(root)
    assert created["root_id"] == root_id


@pytest.mark.live
@pytest.mark.mcp
def test_vault_create_folder_refuses_to_escape_the_root(mcp, comfy_root):
    root_id, _root = comfy_root
    refused = mcp.call("vault_create_folder",
                       {"root_id": root_id, "folder": "../escaped_probe"})
    assert error_text(refused)


# =============================================================================
# vault_rename and vault_move
# =============================================================================

@pytest.mark.live
@pytest.mark.mcp
def test_vault_rename_renames_the_file_on_disk(probes, mcp, probe):
    new_name = probe.stem + "_renamed.json"
    payload = mcp.call_ok("vault_rename", {"uid": probe.uid, "new_name": new_name})
    assert_keys_declared("vault_rename", payload)
    assert payload["requested"] == 1
    assert payload["affected"] == 1
    assert payload["failed"] == 0
    entry = payload["results"][0]
    assert entry["ok"] is True
    renamed = track(probes, entry["path"])
    assert renamed.is_file()
    assert renamed.name == new_name
    assert not probe.path.exists(), "the old path survived the rename"


@pytest.mark.live
@pytest.mark.mcp
def test_vault_move_relocates_the_probe(probes, mcp, probe, probe_folder):
    payload = mcp.call_ok("vault_move", {
        "uids": [probe.uid], "target_root_id": probe.root_id,
        "target_folder": probe_folder, "create_missing": True,
        "on_conflict": "keep_both"})
    assert_keys_declared("vault_move", payload)
    assert payload["affected"] == 1
    moved = track(probes, payload["results"][0]["path"])
    assert moved.is_file()
    assert moved.parent.name == probe_folder.rsplit("/", 1)[1]
    assert not probe.path.exists(), "the source path survived the move"


@pytest.mark.live
@pytest.mark.mcp
def test_vault_move_reports_a_missing_uid_instead_of_guessing(mcp, comfy_root,
                                                             probe_folder):
    root_id, _root = comfy_root
    result = mcp.call("vault_move", {
        "uids": ["workflow:999999999"], "target_root_id": root_id,
        "target_folder": probe_folder})
    if result.get("isError"):
        assert error_text(result)
        return
    payload = result["structuredContent"]
    assert payload["affected"] == 0
    assert payload["failed"] == 1


# =============================================================================
# vault_assign_tags
# =============================================================================

@pytest.mark.live
@pytest.mark.mcp
def test_vault_assign_tags_round_trips_on_the_probe(mcp, rest, probe):
    tag = f"{PROBE_PREFIX}tag_{uuid.uuid4().hex[:6]}"
    try:
        added = mcp.call_ok("vault_assign_tags", {"uids": [probe.uid], "add": [tag]})
        assert_keys_declared("vault_assign_tags", added)
        assert added["requested"] == 1
        assert added["updated"] >= 1
        assert added["added"] == [tag]
        assert tag in tags_of(probe.uid)
    finally:
        removed = mcp.call_ok("vault_assign_tags",
                              {"uids": [probe.uid], "remove": [tag]})
        assert removed["removed"] == [tag]
        # ...and take the now-unused tag row itself back out of the vault.
        drop_tag(rest, tag)
    assert tag not in tags_of(probe.uid)
    assert tag not in [i["name"] for i in
                       rest.get("/tags", params={"limit": 200}).json()["items"]]


@pytest.mark.live
@pytest.mark.mcp
def test_vault_assign_tags_needs_something_to_do(mcp, probe):
    assert error_text(mcp.call("vault_assign_tags", {"uids": [probe.uid]}))


# =============================================================================
# vault_delete / vault_trash_list / vault_trash_restore
# =============================================================================

@pytest.mark.live
@pytest.mark.mcp
def test_delete_trashes_then_restores_then_permanently_deletes(probes, mcp, rest,
                                                               probe):
    """The full recoverable-delete cycle, on one disposable file.

    Trash is the default; the entry is listed; restoring puts the bytes back;
    only an explicit ``confirm`` erases them.
    """
    deleted = mcp.call_ok("vault_delete", {"uids": [probe.uid]})
    assert_keys_declared("vault_delete", deleted)
    entry = deleted["results"][0]
    assert deleted["affected"] == 1
    assert entry["mode"] == "trash", "delete must default to the recoverable mode"
    assert not probe.path.exists()
    trash_path = Path(entry["trash_path"])
    trash_id = entry["trash_id"]
    assert trash_path.exists(), "the bytes did not reach the trash"

    restored_from_trash = False
    try:
        listed = mcp.call_ok("vault_trash_list", {"limit": 50})
        assert_keys_declared("vault_trash_list", listed)
        hit = [item for item in listed["items"] if item["id"] == trash_id]
        assert hit, f"trash entry {trash_id} is not listed"
        assert hit[0]["kind"] == "workflow"

        restored = mcp.call_ok("vault_trash_restore", {"ids": [trash_id]})
        assert_keys_declared("vault_trash_restore", restored)
        assert restored["affected"] == 1
        live_path = track(probes, restored["results"][0]["path"])
        assert live_path.is_file(), "restore did not put the file back"
        assert not trash_path.exists()
        restored_from_trash = True
    finally:
        if not restored_from_trash:
            # Never leave a probe entry sitting in the owner's trash.
            mcp.call("vault_trash_empty", {"ids": [trash_id], "confirm": True})

    # The row may be rebuilt inline or by the next scan; file_ops documents the
    # rescan fallback, so ask the index rather than assuming.
    rescan(mcp, rest)
    found = mcp.call_ok("list_workflows", {"name_contains": probe.stem, "limit": 5})
    assert found["workflows"], "the restored workflow was not re-indexed"
    uid = found["workflows"][0]["uid"]

    purged = mcp.call_ok("vault_delete", {"uids": [uid], "mode": "permanent",
                                          "confirm": True})
    assert purged["affected"] == 1
    assert purged["results"][0]["mode"] == "permanent"
    assert not live_path.exists()


@pytest.mark.live
@pytest.mark.mcp
def test_permanent_delete_without_confirm_is_refused(mcp, probe):
    """The file must still be there afterwards -- that is the whole point."""
    refused = mcp.call("vault_delete", {"uids": [probe.uid], "mode": "permanent"})
    assert "confirm=true" in error_text(refused)
    assert probe.path.is_file(), "a refused permanent delete removed the file"


@pytest.mark.live
@pytest.mark.mcp
def test_a_batch_over_the_cap_is_refused_before_anything_happens(mcp):
    """201 uids, all of them fictional: the cap has to fire on the *count*."""
    from app.mcp import registry

    # Ids far past anything the vault holds: if the cap ever regressed, this
    # call still could not reach a real file.
    uids = [f"workflow:{900_000_000 + i}" for i in range(registry.MAX_BATCH + 1)]
    refused = mcp.call("vault_delete", {"uids": uids})
    assert str(registry.MAX_BATCH) in error_text(refused)


# =============================================================================
# vault_trash_empty
# =============================================================================

@pytest.mark.live
@pytest.mark.mcp
def test_trash_empty_without_confirm_is_refused(mcp):
    refused = mcp.call("vault_trash_empty", {"older_than_days": ANCIENT_DAYS,
                                             "confirm": False})
    assert "confirm=true" in error_text(refused)


@pytest.mark.live
@pytest.mark.mcp
def test_trash_empty_with_confirm_purges_only_the_chosen_window(mcp):
    """A ten-year window cannot contain anything a live vault put in the trash,
    so this proves the plumbing without destroying a single recoverable file."""
    before = mcp.call_ok("vault_trash_list", {"limit": 200})
    cutoff_ms = time.time() * 1000 - ANCIENT_DAYS * 86_400_000
    ancient = [i for i in before["items"] if (i.get("deleted_at") or 0) < cutoff_ms]
    if ancient:
        pytest.skip("the vault holds a trash entry older than the probe window")
    purged = mcp.call_ok("vault_trash_empty", {"older_than_days": ANCIENT_DAYS,
                                               "confirm": True})
    assert_keys_declared("vault_trash_empty", purged)
    assert purged["removed"] == 0
    assert purged["files_removed"] == 0
    after = mcp.call_ok("vault_trash_list", {"limit": 200})
    assert after["total"] == before["total"]


# =============================================================================
# The promoted job-control tools
# =============================================================================

@pytest.mark.live
@pytest.mark.mcp
def test_hash_enqueue_with_an_empty_scope_is_not_an_error(mcp):
    """Deliberately scoped to nothing: hashing a real model would write a hash
    the vault did not have before, which this suite is not allowed to do."""
    queued = mcp.call_ok("vault_hash_enqueue",
                         {"scope": "category", "category": "zz_no_such_category"})
    assert queued["queued"] == 0
    assert queued["message"]


@pytest.mark.live
@pytest.mark.mcp
def test_hash_enqueue_without_a_category_is_refused(mcp):
    assert error_text(mcp.call("vault_hash_enqueue", {"scope": "category"}))


@pytest.mark.live
@pytest.mark.mcp
def test_hash_cancel_on_an_unknown_batch_reports_nothing_cancelled(mcp):
    cancelled = mcp.call_ok("vault_hash_cancel", {"batch_id": "zz-no-such-batch"})
    assert cancelled["cancelled"] == 0


# =============================================================================
# The audit trail (DECISIONS C5 rail 3)
# =============================================================================

@pytest.mark.live
@pytest.mark.mcp
def test_mutations_are_audited_and_reads_are_not(probes, mcp, probe):
    """Rail 3: every mutation is logged *with its argument values*, refusals
    included, and no read tool ever writes a row."""
    before = {row["id"] for row in audit_rows(mcp.session_id)}

    mcp.call_ok("list_workflows", {"limit": 1})
    mcp.call_ok("vault_trash_list", {"limit": 1})
    new_name = probe.stem + "_audited.json"
    renamed = mcp.call_ok("vault_rename", {"uid": probe.uid, "new_name": new_name})
    track(probes, renamed["results"][0]["path"])
    refused = mcp.call("vault_delete", {"uids": [probe.uid], "mode": "permanent"})
    assert refused["isError"] is True

    rows = [r for r in audit_rows(mcp.session_id) if r["id"] not in before]
    by_tool = {r["tool"]: r for r in rows}

    assert "vault_rename" in by_tool
    assert "vault_delete" in by_tool
    assert "list_workflows" not in by_tool
    assert "vault_trash_list" not in by_tool

    rename_row = by_tool["vault_rename"]
    assert rename_row["outcome"] == "ok"
    assert rename_row["transport"] == "http"
    assert rename_row["affected"] == 1
    # The VALUES, not just the argument names: an audit that says "rename was
    # called" without saying what it renamed is not an audit.
    assert json.loads(rename_row["arguments"])["new_name"] == new_name
    assert probe.uid in json.loads(rename_row["uids"])
    assert rename_row["elapsed_ms"] is not None

    refused_row = by_tool["vault_delete"]
    assert refused_row["outcome"] == "error"
    assert refused_row["error_code"]
    assert refused_row["affected"] == 0


@pytest.mark.live
@pytest.mark.mcp
def test_the_audit_row_id_is_handed_back_to_the_caller(mcp, rest, probe):
    """``audit_id`` on the payload is how an operator ties a change in the
    library back to the call that made it."""
    tag = f"{PROBE_PREFIX}audit_{uuid.uuid4().hex[:6]}"
    payload = mcp.call_ok("vault_assign_tags", {"uids": [probe.uid], "add": [tag]})
    try:
        assert isinstance(payload["audit_id"], int)
        rows = {r["id"]: r for r in audit_rows(mcp.session_id)}
        assert rows[payload["audit_id"]]["tool"] == "vault_assign_tags"
    finally:
        mcp.call_ok("vault_assign_tags", {"uids": [probe.uid], "remove": [tag]})
        drop_tag(rest, tag)


@pytest.mark.live
@pytest.mark.mcp
def test_no_probe_tag_survives_this_module(rest):
    """Defence in depth: even an unused tag row is a change to the vault."""
    names = [item["name"] for item in
             rest.get("/tags", params={"limit": 200}).json()["items"]]
    leftovers = [name for name in names if name.startswith(PROBE_PREFIX)]
    for name in leftovers:
        drop_tag(rest, name)
    assert not leftovers, f"probe tag(s) left behind: {leftovers}"
