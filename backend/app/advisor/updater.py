"""The catalog refresh job - manual, or self-initiated when the data is stale.

One background thread, one run at a time.  A run asks each source in turn,
isolates their failures from one another, and writes a new catalog only at the
end, so a network drop half-way leaves the previous catalog untouched.

How it finds models, in order:

1. **Pins** - repositories named in the source list, plus any the owner added.
2. **Discovery** - per-task "most downloaded" and "trending" listings, and the
   newest work from recognised publishers.
3. **Following links** - a quantised or repackaged repository names the model it
   was made from (``base_model``); the updater follows that one hop so the
   original lands in the catalog too, and remembers it for next time.
4. **Its own source list** - a newer ``sources.json`` in this project's
   repository replaces the built-in one, so pins and queries improve without a
   new release.  It is data only, shape-checked, and cannot add a host.

What it keeps between runs: when each source last answered and how many entries
it gave, which repositories discovery learned, and when every entry was first
seen - which is how the interface marks what is new.

Kill-switch: nothing here opens a socket unless ``online_enabled`` is on.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..core import config_service
from ..core import db as dbmod
from ..core.errors import AppError, ConflictError, FeatureUnavailable, ValidationError
from . import catalog, fit, sources

log = logging.getLogger("vault.advisor.updater")

#: A catalog older than this is refreshed automatically (when allowed).
STALE_AFTER_MS = 24 * 60 * 60 * 1000
#: Never start two automatic runs closer together than this.
AUTO_MIN_GAP_MS = 6 * 60 * 60 * 1000
DETAIL_WORKERS = 4
MAX_HF_DETAILS = 420
REQUEST_PAUSE_S = 0.05

SOURCE_IDS = ("vault", "huggingface", "civitai", "comfy_manager", "openmodeldb",
              "ollama", "github")

_lock = threading.RLock()
_thread: threading.Thread | None = None
_cancel = threading.Event()
_status: dict = {"running": False, "phase": None, "phase_label": None,
                 "done": 0, "total": 0, "started_at": None, "finished_at": None,
                 "trigger": None, "error": None, "result": None}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _set(**patch) -> None:
    with _lock:
        _status.update(patch)


def status() -> dict:
    cfg = config_service.get_config()
    cat = catalog.load()
    state = catalog.load_state()
    age = dbmod.now_ms() - cat["generated_at"] if cat["generated_at"] else None
    with _lock:
        snapshot = dict(_status)
    snapshot.update({
        "online_enabled": bool(cfg.online_enabled),
        "auto_update": bool(cfg.raw.get("advisor_auto_update", True)),
        "catalog_origin": cat["origin"],
        "catalog_generated_at": cat["generated_at"] or None,
        "catalog_age_ms": age,
        "stale": age is None or age > STALE_AFTER_MS,
        "stale_after_ms": STALE_AFTER_MS,
        "entries": len(cat["entries"]),
        "last_run": state.get("last_run"),
    })
    return snapshot


def is_running() -> bool:
    with _lock:
        return bool(_status["running"])


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------

def _require_online() -> None:
    if not config_service.get_config().online_enabled:
        raise FeatureUnavailable(
            "Outbound lookups are disabled, so the model catalog cannot be refreshed.",
            details={"reason": "online_disabled",
                     "hint": "Settings -> Search -> Allow outbound lookups at all"})


def start(*, trigger: str = "manual", kinds: list[str] | None = None,
          only: list[str] | None = None) -> dict:
    """Begin a refresh in the background.  Raises if one is already running."""
    global _thread
    _require_online()
    wanted_kinds = [k for k in (kinds or []) if k in fit.KINDS] or None
    wanted_sources = [s for s in (only or []) if s in SOURCE_IDS] or None
    with _lock:
        if _status["running"]:
            raise ConflictError("A catalog refresh is already running.")
        _cancel.clear()
        _status.update({"running": True, "phase": "starting",
                        "phase_label": "Starting", "done": 0, "total": 0,
                        "started_at": dbmod.now_ms(), "finished_at": None,
                        "trigger": trigger, "error": None, "result": None})
        _thread = threading.Thread(target=_run, name="advisor-update",
                                   args=(trigger, wanted_kinds, wanted_sources),
                                   daemon=True)
        _thread.start()
    return status()


def cancel() -> dict:
    _cancel.set()
    return status()


def maybe_auto_update() -> str:
    """Self-initiated refresh: only when allowed, stale, and not just tried."""
    cfg = config_service.get_config()
    if not cfg.online_enabled:
        return "offline"
    if not cfg.raw.get("advisor_auto_update", True):
        return "disabled"
    if is_running():
        return "running"
    cat = catalog.load()
    now = dbmod.now_ms()
    if cat["generated_at"] and now - cat["generated_at"] < STALE_AFTER_MS:
        return "fresh"
    last = (catalog.load_state().get("last_run") or {}).get("finished_at") or 0
    if now - int(last) < AUTO_MIN_GAP_MS:
        return "recently_tried"
    try:
        start(trigger="auto", kinds=list(cfg.raw.get("advisor_kinds") or []) or None)
    except AppError as exc:
        log.info("automatic catalog refresh not started: %s", exc.message)
        return "skipped"
    return "started"


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

class _Cancelled(Exception):
    pass


def _check_cancel() -> None:
    if _cancel.is_set():
        raise _Cancelled()


def _source_step(report: dict, source_id: str, label: str, func) -> list:
    """Run one source, recording its outcome; a failure never stops the run."""
    _check_cancel()
    _set(phase=source_id, phase_label=label, done=0, total=0)
    started = time.monotonic()
    row = {"ok": False, "entries": 0, "error": None, "at": dbmod.now_ms()}
    result: list = []
    try:
        result = func() or []
        row.update(ok=True, entries=len(result))
    except _Cancelled:
        raise
    except AppError as exc:
        row["error"] = exc.message
        log.warning("catalog source %s failed: %s", source_id, exc.message)
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("catalog source %s failed", source_id, exc_info=True)
    row["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    report[source_id] = row
    return result


def _refresh_source_list(client, state: dict) -> list:
    status_code, doc = sources.fetch_json(sources.VAULT_SOURCES, client=client,
                                          accept_statuses=(404,))
    if status_code == 404 or not catalog.valid_sources_doc(doc):
        return []
    current = catalog.sources()
    if doc["revision"] > current["revision"]:
        # Written through a fresh read, never the copy this run started with:
        # the owner may have added a pin since.
        latest = catalog.load_state()
        latest["remote_sources"] = doc
        catalog.save_state(latest)
        state["remote_sources"] = doc
        log.info("source list updated to revision %s", doc["revision"])
        return [doc]
    return []


def _hf(client, defs: dict, state: dict, previous: dict[str, dict],
        kinds: list[str] | None, flags: dict) -> list[dict]:
    trust = defs.get("publishers") or {}
    hints: dict[str, dict] = {}
    listed_sha: dict[str, str | None] = {}

    def want(kind: str | None) -> bool:
        return not kinds or kind is None or kind in kinds

    for pin in list((defs.get("pins") or {}).get("huggingface") or []) \
            + list(defs.get("user_pins") or []):
        repo = str(pin.get("repo") or "")
        if sources.valid_repo_id(repo) and want(pin.get("kind")):
            hints[repo] = dict(pin)
    for repo in defs.get("learned") or []:
        if sources.valid_repo_id(repo):
            hints.setdefault(repo, {})

    queries = [q for q in (defs.get("discovery") or {}).get("huggingface") or []
               if want(q.get("kind"))]
    _set(total=len(queries))
    failed_listings = 0
    last_error: AppError | None = None
    for index, query in enumerate(queries):
        _check_cancel()
        try:
            _status_code, rows = sources.fetch_json(sources.hf_list_url(query),
                                                    client=client)
        except AppError as exc:
            log.info("hub listing failed (%s): %s", query, exc.message)
            rows = []
            failed_listings += 1
            last_error = exc
        if failed_listings == len(queries) == index + 1 and last_error is not None:
            # Not one listing answered: the Hub is unreachable.  Say so, so the
            # run keeps the previous Hub entries instead of replacing them with
            # the handful of pins it could reuse from memory.
            raise last_error
        for row in rows if isinstance(rows, list) else []:
            repo = str(row.get("id") or "")
            if not sources.valid_repo_id(repo):
                continue
            listed_sha[repo] = row.get("sha")
            hint = hints.setdefault(repo, {})
            if query.get("kind") and not hint.get("kind"):
                hint["kind"] = query["kind"]
        _set(done=index + 1)
        time.sleep(REQUEST_PAUSE_S)

    def fetch(repo: str) -> dict | None:
        if _cancel.is_set():
            return None
        old = previous.get(f"hf:{repo}")
        sha = listed_sha.get(repo)
        if old and sha and old.get("revision") == sha:
            return old  # unchanged upstream - no request needed
        try:
            status_code, detail = sources.fetch_json(
                sources.hf_detail_url(repo), client=client, accept_statuses=(401, 403, 404))
        except AppError:
            return old
        if not isinstance(detail, dict):
            if status_code == 404:
                return None
            return old
        return sources.normalize_hf(detail, hint=hints.get(repo), trust=trust)

    def fetch_all(repos: list[str], label: str) -> list[dict]:
        _set(phase_label=label, done=0, total=len(repos))
        out: list[dict] = []
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS,
                                thread_name_prefix="advisor-hf") as pool:
            for index, entry in enumerate(pool.map(fetch, repos)):
                if entry:
                    out.append(entry)
                _set(done=index + 1)
        _check_cancel()
        return out

    ordered = sorted(hints, key=lambda r: (not hints[r].get("curated"), r))[:MAX_HF_DETAILS]
    entries = fetch_all(ordered, "Hugging Face - reading model files")

    follow = (defs.get("discovery") or {}).get("follow_base_models") or {}
    if follow.get("enabled", True):
        have = {e["id"] for e in entries}
        upstream: list[str] = []
        for entry in entries:
            base = entry.get("base_model")
            if base and sources.valid_repo_id(base) and f"hf:{base}" not in have \
                    and base not in upstream and base not in hints:
                hints[base] = {"kind": entry["kind"]}
                upstream.append(base)
        upstream = upstream[:max(0, min(80, int(follow.get("limit") or 30)))]
        if upstream:
            found = fetch_all(upstream, "Hugging Face - following base models")
            entries += found
            learned = list(state.get("learned") or [])
            for entry in found:
                repo = entry["id"][3:]
                if repo not in learned:
                    learned.append(repo)
            state["learned"] = learned
    if kinds:
        entries = [e for e in entries if e["kind"] in kinds]
    if failed_listings:
        # Some listings answered and some did not: what they would have
        # discovered is unknown, so nothing already known may be dropped.
        flags["partial"] = True
    return entries


def _civitai(client, defs: dict, kinds: list[str] | None) -> list[dict]:
    queries = (defs.get("discovery") or {}).get("civitai") or []
    _set(total=len(queries))
    seen: dict[str, dict] = {}
    for index, query in enumerate(queries):
        _check_cancel()
        kind = sources.CIVITAI_KIND.get(str(query.get("types") or "").lower())
        if kinds and kind not in kinds and not (kind == "image" and "video" in kinds):
            _set(done=index + 1)
            continue
        _status_code, doc = sources.fetch_json(sources.civitai_list_url(query),
                                               client=client)
        for item in (doc or {}).get("items") or [] if isinstance(doc, dict) else []:
            entry = sources.normalize_civitai(item)
            if entry and (not kinds or entry["kind"] in kinds):
                seen.setdefault(entry["id"], entry)
        _set(done=index + 1)
        time.sleep(REQUEST_PAUSE_S)
    return list(seen.values())


def _comfy_manager(client, kinds: list[str] | None) -> list[dict]:
    _status_code, doc = sources.fetch_json(sources.COMFY_MANAGER_LIST, client=client)
    out: dict[str, dict] = {}
    for item in (doc or {}).get("models") or [] if isinstance(doc, dict) else []:
        entry = sources.normalize_comfy_manager(item) if isinstance(item, dict) else None
        if entry and entry["kind"] != "other" and (not kinds or entry["kind"] in kinds):
            out.setdefault(entry["id"], entry)
    return list(out.values())


def _openmodeldb(client, defs: dict, kinds: list[str] | None) -> list[dict]:
    if kinds and "upscale" not in kinds:
        return []
    _status_code, doc = sources.fetch_json(sources.OPENMODELDB_LIST, client=client)
    limit = int(((defs.get("discovery") or {}).get("openmodeldb") or {}).get("limit") or 120)
    rows = []
    for model_id, item in (doc or {}).items() if isinstance(doc, dict) else []:
        entry = sources.normalize_openmodeldb(model_id, item)
        if entry:
            rows.append(entry)
    rows.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    return rows[:max(1, min(400, limit))]


def _ollama(client, kinds: list[str] | None) -> list[dict]:
    if kinds and "llm" not in kinds:
        return []
    _status_code, doc = sources.fetch_json(sources.OLLAMA_TAGS, client=client)
    out = []
    for item in (doc or {}).get("models") or [] if isinstance(doc, dict) else []:
        entry = sources.normalize_ollama(item) if isinstance(item, dict) else None
        if entry:
            out.append(entry)
    return out


def _github(client, defs: dict) -> list[dict]:
    leads: dict[str, dict] = {}
    for query in (defs.get("discovery") or {}).get("github") or []:
        _check_cancel()
        try:
            url = sources.github_search_url(query)
        except ValueError:
            continue
        _status_code, doc = sources.fetch_json(url, client=client,
                                               accept_statuses=(403, 422, 429))
        for repo in (doc or {}).get("items") or [] if isinstance(doc, dict) else []:
            lead = sources.normalize_github_lead(repo)
            if lead:
                leads.setdefault(lead["url"], lead)
        time.sleep(0.5)  # the unauthenticated search limit is ten a minute
    return sorted(leads.values(), key=lambda r: r.get("stars") or 0, reverse=True)[:40]


def _dedupe(entries: list[dict]) -> list[dict]:
    """Drop a curated-list row when the Hub already describes the same file."""
    hub_files = {v.get("download_url") for e in entries if e["source"] == "huggingface"
                 for v in e.get("variants") or []}
    hub_files |= {c.get("download_url") for e in entries if e["source"] == "huggingface"
                  for c in e.get("companions") or []}
    out: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        if entry["id"] in seen:
            continue
        if entry["source"] == "comfy_manager" and \
                entry["variants"][0].get("download_url") in hub_files:
            continue
        seen.add(entry["id"])
        out.append(entry)
    return out


def _run(trigger: str, kinds: list[str] | None, only: list[str] | None) -> None:
    import httpx

    started = dbmod.now_ms()
    report: dict = {}
    previous_catalog = catalog.load()
    previous = dict(previous_catalog["by_id"])
    state = catalog.load_state()
    error = None
    result = None

    def enabled(source_id: str) -> bool:
        return not only or source_id in only

    try:
        with httpx.Client(timeout=sources.TIMEOUT_S, follow_redirects=False,
                          headers={"User-Agent": sources.USER_AGENT,
                                   "Accept": "application/json"}) as client:
            if enabled("vault"):
                _source_step(report, "vault", "Checking the source list",
                             lambda: _refresh_source_list(client, state))
            defs = catalog.sources()
            fresh: dict[str, list[dict]] = {}
            hub_flags: dict = {}
            if enabled("huggingface"):
                fresh["huggingface"] = _source_step(
                    report, "huggingface", "Hugging Face - listing models",
                    lambda: _hf(client, defs, state, previous, kinds, hub_flags))
                if hub_flags.get("partial") and "huggingface" in report:
                    report["huggingface"]["partial"] = True
            if enabled("civitai"):
                fresh["civitai"] = _source_step(
                    report, "civitai", "Civitai", lambda: _civitai(client, defs, kinds))
            if enabled("comfy_manager"):
                fresh["comfy_manager"] = _source_step(
                    report, "comfy_manager", "ComfyUI-Manager model list",
                    lambda: _comfy_manager(client, kinds))
            if enabled("openmodeldb"):
                fresh["openmodeldb"] = _source_step(
                    report, "openmodeldb", "OpenModelDB",
                    lambda: _openmodeldb(client, defs, kinds))
            if enabled("ollama"):
                fresh["ollama"] = _source_step(
                    report, "ollama", "Ollama library", lambda: _ollama(client, kinds))
            leads = previous_catalog.get("leads") or []
            if enabled("github"):
                found = _source_step(report, "github", "GitHub - new projects",
                                     lambda: _github(client, defs))
                if report["github"]["ok"] and found:
                    leads = found

        _set(phase="writing", phase_label="Writing the catalog", done=0, total=0)
        # A source that failed, was skipped, or was limited to some kinds keeps
        # what it had: a partial run never shrinks the catalog.
        merged: dict[str, dict] = {}
        for entry in previous_catalog["entries"]:
            source_id = str(entry.get("source"))
            row = report.get(source_id)
            replaced = bool(row and row["ok"] and not row.get("partial")) \
                and (not kinds or entry["kind"] in kinds)
            if not replaced:
                merged[entry["id"]] = entry
        for rows in fresh.values():
            for entry in rows:
                merged[entry["id"]] = entry
        entries = _dedupe(list(merged.values()))

        first_seen = dict(state.get("first_seen") or {})
        baseline = not first_seen
        now = dbmod.now_ms()
        for entry in entries:
            if entry["id"] not in first_seen:
                # On the very first run everything is "already known", so the
                # whole catalog is not flagged as new.
                first_seen[entry["id"]] = 0 if baseline else now
        first_seen = {k: v for k, v in first_seen.items() if k in merged}
        header = catalog.save(entries, sources=report, leads=leads)
        new_count = sum(1 for e in entries if first_seen.get(e["id"], 0) >= started)
        result = {"entries": header["entries"], "new": new_count,
                  "generated_at": header["generated_at"],
                  "counts": catalog.counts(entries),
                  "failed_sources": [k for k, v in report.items() if not v["ok"]]}
        state["first_seen"] = first_seen
    except _Cancelled:
        error = "Cancelled."
    except Exception as exc:
        log.exception("catalog refresh failed")
        error = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        finished = dbmod.now_ms()
        # Merge into the state as it is NOW.  The run took a minute; writing
        # back the copy it started with would silently undo a pin the owner
        # added or removed meanwhile.  Only the fields a run owns are touched.
        latest = catalog.load_state()
        for source_id, row in report.items():
            latest["sources"][source_id] = row
        latest["last_run"] = {"started_at": started, "finished_at": finished,
                              "trigger": trigger, "error": error,
                              "result": result, "kinds": kinds}
        if result is not None:
            latest["first_seen"] = state.get("first_seen") or {}
        learned = list(latest.get("learned") or [])
        learned += [r for r in state.get("learned") or [] if r not in learned]
        latest["learned"] = learned
        state = latest
        try:
            catalog.save_state(state)
        except OSError:
            log.warning("could not save advisor state", exc_info=True)
        _set(running=False, phase=None, phase_label=None, finished_at=finished,
             error=error, result=result)
        log.info("catalog refresh (%s) finished: %s", trigger, error or result)


# ---------------------------------------------------------------------------
# Owner-added pins
# ---------------------------------------------------------------------------

def add_pin(repo: str, kind: str | None = None) -> dict:
    """Remember a Hugging Face repository the owner wants in the catalog."""
    repo = str(repo or "").strip().removeprefix("https://huggingface.co/").strip("/")
    if not sources.valid_repo_id(repo):
        raise ValidationError(
            "Give a Hugging Face repository as owner/name.",
            details={"field": "repo"})
    if kind is not None and kind not in fit.KINDS:
        raise ValidationError("Unknown model type.", details={"field": "kind"})
    state = catalog.load_state()
    pins = [p for p in state.get("user_pins") or [] if p.get("repo") != repo]
    if len(pins) >= catalog.MAX_USER_PINS:
        raise ValidationError("The pin list is full; remove one first.")
    pin = {"repo": repo, "added_at": dbmod.now_ms()}
    if kind:
        pin["kind"] = kind
    pins.append(pin)
    state["user_pins"] = pins
    catalog.save_state(state)
    return pin


def remove_pin(repo: str) -> bool:
    state = catalog.load_state()
    pins = state.get("user_pins") or []
    kept = [p for p in pins if p.get("repo") != repo]
    if len(kept) == len(pins):
        return False
    state["user_pins"] = kept
    catalog.save_state(state)
    return True
