"""Where a download URL is allowed to come from.

**No caller ever names a source.**  A URL reaches the fetcher from exactly three
places, all of them already on this machine:

1. The workflow's own model manifest.  ComfyUI records ``{name, url, hash,
   hash_type, directory}`` per model - at the top level of an exported graph, in
   ``extra.models``, and on each node as ``properties.models``.  This is how an
   official template says "this graph needs these weights, here is where they
   came from", and it is the only place a *specific* download URL for a missing
   model legitimately exists.
2. A ``download_url`` the vault already cached on an indexed model row.  Civitai
   enrichment writes it, keyed on the file's SHA-256, so it is available for a
   model the vault has seen before and the user has since moved or removed.
3. The ComfyUI-Manager registry (``extension-node-map.json``), for the git
   remote of a node package.

There is deliberately **no filename lookup against any API**.  ARCHITECTURE 8.4
states that outbound requests carry only hashes and repo names - never
filenames, paths or prompts - and asking Civitai "who has a file called X?"
would leak exactly what that rule protects.  A missing model with no declared
source is *reported* with its resolved destination and the reason, not guessed
at.

Every URL out of every one of these paths is still validated by ``hosts.check``
before it is used.  Being local does not make it trusted: a workflow file is
third-party content.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..core import db as dbmod
from ..parsers import node_registry
from . import hosts

log = logging.getLogger(__name__)

#: A manifest entry may carry any of these keys for its hash.
_HASH_KEYS = ("hash", "sha256", "SHA256", "checksum")
_HASH_TYPE_KEYS = ("hash_type", "hashType", "algorithm")

MAX_MANIFEST_ENTRIES = 500


@dataclass
class Source:
    """One validated place a file may be fetched from."""

    url: str
    host: str
    provider: str                    # workflow_manifest | vault_cache
    expected_size: int = 0
    expected_sha256: str | None = None
    declared_directory: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "url": self.url, "host": self.host, "provider": self.provider,
            "size": self.expected_size,
            "sha256": self.expected_sha256,
            "hash_available": bool(self.expected_sha256),
            "declared_directory": self.declared_directory,
            "notes": list(self.notes),
        }


def _norm_name(value: Any) -> str:
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip().lower()


def _as_int(value: Any) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _sha_of(entry: dict) -> str | None:
    kind = ""
    for key in _HASH_TYPE_KEYS:
        if entry.get(key):
            kind = str(entry[key]).strip().upper()
            break
    raw = ""
    for key in _HASH_KEYS:
        if entry.get(key):
            raw = str(entry[key]).strip()
            break
    if not raw:
        return None
    text = raw.lower()
    if len(text) == 64 and all(c in "0123456789abcdef" for c in text):
        # A bare 64-hex digest is SHA-256 whether or not the file said so.
        if kind and kind not in ("SHA256", "SHA-256", "SHA_256"):
            return None
        return text
    return None


def _iter_manifest_entries(graph: Any) -> list[dict]:
    """Collect every ``models`` manifest the graph carries, in any of its shapes."""
    out: list[dict] = []

    def take(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and len(out) < MAX_MANIFEST_ENTRIES:
                    out.append(item)

    if isinstance(graph, dict):
        take(graph.get("models"))
        extra = graph.get("extra")
        if isinstance(extra, dict):
            take(extra.get("models"))
        nodes = graph.get("nodes")
        if isinstance(nodes, list):
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                props = node.get("properties")
                if isinstance(props, dict):
                    take(props.get("models"))
        # API-format graphs keep nodes in a dict keyed by node id instead.
        if not isinstance(graph.get("nodes"), list):
            for node in graph.values():
                if isinstance(node, dict) and isinstance(node.get("properties"), dict):
                    take(node["properties"].get("models"))
    return out


def manifest_index(graph: Any) -> dict[str, Source]:
    """``normalized filename -> Source`` for every usable manifest entry.

    Entries whose URL is not allowlisted are dropped here with a log line and
    never reach the report as something fetchable - the report shows the refusal
    reason instead, so the user learns *why* rather than seeing nothing.
    """
    index: dict[str, Source] = {}
    for entry in _iter_manifest_entries(graph):
        name = _norm_name(entry.get("name") or entry.get("filename"))
        url = entry.get("url") or entry.get("download_url")
        if not name or not url:
            continue
        try:
            checked = hosts.check(str(url), kind=hosts.KIND_MODEL)
        except hosts.HostNotAllowed as exc:
            index.setdefault(name, Source(
                url="", host="", provider="workflow_manifest",
                notes=[f"declared source refused: {exc.reason}"]))
            continue
        directory = entry.get("directory") or entry.get("folder")
        source = Source(
            url=checked.url, host=checked.host, provider="workflow_manifest",
            expected_size=_as_int(entry.get("size") or entry.get("bytes")
                                  or entry.get("size_bytes")),
            expected_sha256=_sha_of(entry),
            declared_directory=str(directory) if directory else None,
        )
        index[name] = source
    return index


def vault_cached_source(ref_name: str,
                        conn: sqlite3.Connection | None = None) -> Source | None:
    """A ``download_url`` the vault already holds for a file of this name.

    Written by Civitai enrichment, which is keyed on the SHA-256 of a file the
    vault hashed itself.  No network call is made to obtain it.
    """
    conn = conn or dbmod.get_ro()
    base = _norm_name(ref_name)
    if not base:
        return None
    row = dbmod.one(
        conn,
        "SELECT m.download_url, f.size, f.sha256 FROM models m "
        "JOIN model_files f ON f.model_id = m.id "
        "WHERE LOWER(f.filename) = ? AND m.download_url IS NOT NULL "
        "ORDER BY f.missing_since IS NULL DESC, f.id LIMIT 1",
        (base,),
    )
    if row is None or not row["download_url"]:
        return None
    try:
        checked = hosts.check(str(row["download_url"]), kind=hosts.KIND_MODEL)
    except hosts.HostNotAllowed:
        return None
    return Source(url=checked.url, host=checked.host, provider="vault_cache",
                  expected_size=_as_int(row["size"]),
                  expected_sha256=(str(row["sha256"]).lower()
                                   if row["sha256"] else None),
                  notes=["from a Civitai record the vault already cached"])


def registry_source(node_class: str, comfy_root) -> dict | None:
    """The git remote the ComfyUI-Manager registry declares for a node class."""
    hint = node_registry.get_registry(comfy_root).package_for_node(str(node_class))
    if not hint:
        return None
    repo = node_registry.normalize_repo_url(hint.get("repo_url"))
    if not repo:
        return None
    out = {"package": hint.get("package") or node_registry.repo_basename(repo),
           "repo_url": repo, "host": None, "allowed": False, "reason": None}
    try:
        checked = hosts.check(repo, kind=hosts.KIND_GIT)
    except hosts.HostNotAllowed as exc:
        out["reason"] = exc.reason
        return out
    out["host"] = checked.host
    out["allowed"] = True
    return out


def workflow_graph_json(workflow_id: int,
                        conn: sqlite3.Connection | None = None) -> Any:
    """The stored graph for a workflow, or ``None`` when it was too large."""
    conn = conn or dbmod.get_ro()
    row = dbmod.one(conn, "SELECT graph_json FROM workflows WHERE id = ?",
                    (int(workflow_id),))
    if row is None or not row["graph_json"]:
        return None
    try:
        return json.loads(row["graph_json"])
    except ValueError:
        return None
