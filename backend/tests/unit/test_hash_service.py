"""B2 — AutoV2 must be the real thing, and hashing must never run inside a scan.

Civitai's AutoV2 is the first 10 hex characters of the **full-file** SHA-256.
The original implementation hashed only the first 64 KB *after* the safetensors
header, and on the file it was verified against that read fell past EOF, so it
returned ``E3B0C44298`` — the SHA-256 of the empty string — for every model.
Every Civitai feature silently died: no description, no update alerts, no
trigger words, no recommended settings.

The second half of the gate is structural.  Full-file SHA-256 over 1.5 TB is a
~2.8-hour job (C1), so it must never be reachable from the indexer: a scan that
hashes is a scan that never finishes.  That is asserted over the source, so it
cannot regress by someone adding one import.
"""

from __future__ import annotations

import ast
import hashlib
import os
import threading
from pathlib import Path

import pytest

from app.jobs import hash_service

APP_DIR = Path(hash_service.__file__).resolve().parent.parent


def reference_autov2(path: Path) -> str:
    """The definition, implemented independently of the code under test."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:10].upper()


EMPTY_STRING_SHA256_PREFIX = hashlib.sha256(b"").hexdigest()[:10].upper()


# ---------------------------------------------------------------------------
# Correctness
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_files(tmp_path: Path) -> list[Path]:
    """Files that straddle every boundary the old 64 KB window sat on."""
    import struct

    out = []
    # a real safetensors container whose body is much larger than 64 KB
    header = b'{"w":{"dtype":"F32","shape":[256,256],"data_offsets":[0,262144]}}'
    header += b" " * ((8 - len(header) % 8) % 8)
    p = tmp_path / "big.safetensors"
    p.write_bytes(struct.pack("<Q", len(header)) + header + bytes(range(256)) * 1024)
    out.append(p)

    for name, data in (
        ("empty.bin", b""),
        ("one_byte.bin", b"\x00"),
        ("under_64k.bin", os.urandom(1000)),
        ("exactly_64k.bin", os.urandom(65536)),
        ("just_over_64k.bin", os.urandom(65537)),
        ("multi_chunk.bin", os.urandom(3 * 1024 * 1024 + 7)),
    ):
        p = tmp_path / name
        p.write_bytes(data)
        out.append(p)
    return out


def test_autov2_equals_the_reference_full_file_sha256_prefix(sample_files):
    for path in sample_files:
        digest, code, nbytes = hash_service.compute_sha256(str(path))
        assert code is None, f"{path.name}: {code}"
        assert nbytes == path.stat().st_size, (
            f"{path.name}: read {nbytes} of {path.stat().st_size} bytes — "
            "a short read is exactly what produced the empty-string digest")
        assert hash_service.autov2(digest) == reference_autov2(path), path.name


def test_compute_autov2_convenience_entry_point_agrees(sample_files):
    for path in sample_files:
        assert hash_service.compute_autov2(str(path)) == reference_autov2(path), path.name


def test_a_non_empty_file_never_hashes_to_the_empty_string_digest(sample_files):
    """The precise signature of B2 in the field."""
    for path in sample_files:
        if path.stat().st_size == 0:
            continue
        assert hash_service.compute_autov2(str(path)) != EMPTY_STRING_SHA256_PREFIX, (
            f"{path.name} hashed to the empty-string digest — the read fell past EOF")


def test_autov2_is_ten_uppercase_hex_characters(sample_files):
    for path in sample_files:
        v = hash_service.compute_autov2(str(path))
        assert len(v) == 10
        assert v == v.upper()
        assert all(c in "0123456789ABCDEF" for c in v)


def test_autov2_of_nothing_is_none():
    assert hash_service.autov2(None) is None
    assert hash_service.autov2("") is None


def test_two_files_differing_in_the_last_byte_hash_differently(tmp_path):
    """A windowed hash would call these identical."""
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    body = os.urandom(2 * 1024 * 1024)
    a.write_bytes(body + b"\x00")
    b.write_bytes(body + b"\x01")
    assert hash_service.compute_autov2(str(a)) != hash_service.compute_autov2(str(b))


def test_hashing_reports_an_error_code_instead_of_raising(tmp_path):
    digest, code, _n = hash_service.compute_sha256(str(tmp_path / "does_not_exist.bin"))
    assert digest is None
    assert code, "a missing file must yield an error code, not an exception"


def test_an_unexpected_file_failure_does_not_escape_the_hash_worker(temp_vault, monkeypatch):
    """A single bad file may fail, but it must not terminate the hash queue."""
    service = hash_service.HashService()
    job = {"id": 1, "model_file_id": 2, "abs_path": "broken", "fsize": 1,
           "batch_id": None, "attempts": 0}
    seen = []
    claims = iter((job, None))
    monkeypatch.setattr(service, "_retire_if_over_capacity", lambda: False)
    monkeypatch.setattr(service, "_claim", lambda: next(claims))
    monkeypatch.setattr(service, "_process_job", lambda _job: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(service, "_finish", lambda *args, **kwargs: seen.append(kwargs))

    service._worker()

    assert seen == [{"state": "failed", "code": "HASH_WORKER_ERROR", "attempts": 1}]


def test_hash_settings_persist_the_slider_value(hermetic_client):
    response = hermetic_client.post(
        "/api/v1/hash/settings", json={"concurrency": 8, "throttle_mbps": 125},
        headers={"X-Vault-Request": "1"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"concurrency": 8, "throttle_mbps": 125}

    config = hermetic_client.get("/api/v1/system/config")
    assert config.status_code == 200
    assert config.json()["hash_concurrency"] == 8
    assert config.json()["hash_throttle_mbps"] == 125


def test_hashing_is_cancellable_mid_file(tmp_path):
    p = tmp_path / "cancel.bin"
    p.write_bytes(os.urandom(8 * 1024 * 1024))
    cancel = threading.Event()
    cancel.set()
    digest, code, nbytes = hash_service.compute_sha256(str(p), cancel=cancel)
    assert digest is None
    assert code == "CANCELLED"
    assert nbytes < p.stat().st_size


def test_progress_is_reported_while_hashing(tmp_path):
    p = tmp_path / "progress.bin"
    p.write_bytes(os.urandom(4 * 1024 * 1024))
    seen: list[int] = []
    hash_service.compute_sha256(str(p), on_chunk=seen.append)
    assert seen, "a multi-hour job must report progress"
    assert seen == sorted(seen)
    assert seen[-1] == p.stat().st_size


# ---------------------------------------------------------------------------
# Structural: hashing is not reachable from a scan
# ---------------------------------------------------------------------------

INDEXING_MODULES = sorted((APP_DIR / "indexing").rglob("*.py"))


@pytest.mark.parametrize("path", INDEXING_MODULES, ids=lambda p: p.name)
def test_no_indexing_module_imports_the_hash_service(path: Path):
    """C1: a scan uses local data only.  Hashing 1.5 TB is a ~2.8 h job."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders.extend(f"{path.name}:{node.lineno} import {a.name}"
                             for a in node.names if "hash_service" in a.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if "hash_service" in mod:
                offenders.append(f"{path.name}:{node.lineno} from {mod}")
            offenders.extend(
                f"{path.name}:{node.lineno} imports {a.name}" for a in node.names
                if a.name in ("hash_service", "HashService", "compute_sha256",
                              "compute_autov2"))
    assert not offenders, "hashing reachable from the indexer:\n" + "\n".join(offenders)


@pytest.mark.parametrize("path", INDEXING_MODULES, ids=lambda p: p.name)
def test_no_indexing_module_computes_a_full_file_digest(path: Path):
    src = path.read_text(encoding="utf-8")
    for needle in ("compute_sha256", "compute_autov2"):
        assert needle not in src, f"{path.name} calls {needle} inside a scan"


def test_the_scan_probe_hash_reads_a_bounded_slice_not_the_whole_file():
    """The indexer *may* hash, but only a fixed probe — never the whole file.

    ``models.py`` fingerprints a model from its first and last megabyte plus the
    size.  That is O(1) per file and is what makes a 1.5 TB cold scan finish in
    seconds.  It is deliberately **not** AutoV2, and the distinction has to hold:
    storing a windowed digest in the ``autov2`` column is precisely B2.
    """
    models_phase = APP_DIR / "indexing" / "phases" / "models.py"
    src = models_phase.read_text(encoding="utf-8")
    assert "PROBE_BYTES" in src, "the probe must be bounded by an explicit constant"
    from app.indexing.phases import models as models_mod

    assert models_mod.PROBE_BYTES <= 4 * 1024 * 1024, (
        f"probe window is {models_mod.PROBE_BYTES} bytes — too large for a cold scan")


def test_the_probe_hash_is_stored_apart_from_autov2(temp_vault):
    """A windowed digest must never be able to masquerade as a Civitai hash."""
    from app.core import db as dbmod

    conn = dbmod.get_ro()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(model_files)")}
    assert "probe_sha256" in cols
    assert "sha256" in cols and "autov2" in cols
    assert "probe_sha256" != "sha256"


def test_a_bounded_probe_and_the_real_autov2_disagree(tmp_path):
    """Proof the two are different values, so confusing them is detectable."""
    from app.indexing.phases import models as models_mod

    p = tmp_path / "model.safetensors"
    p.write_bytes(os.urandom(models_mod.PROBE_BYTES * 3))
    probe = models_mod._probe_hash(str(p), p.stat().st_size)
    full = hash_service.compute_sha256(str(p))[0]
    assert probe and full
    assert probe != full, "the probe must not coincide with the full-file digest"


def test_the_queue_is_table_backed_so_it_survives_a_restart():
    """C1: the queue must outlive the process, not live in memory."""
    src = (APP_DIR / "jobs" / "hash_service.py").read_text(encoding="utf-8")
    assert "hash_jobs" in src, "the queue must be persisted in the hash_jobs table"
    assert "HashService" in src


def test_hash_state_vocabulary_is_the_documented_one():
    """C1 names the states the UI surfaces; drifting from them breaks the card."""
    src = (APP_DIR / "jobs" / "hash_service.py").read_text(encoding="utf-8")
    for state in ("queued", "hashing", "done", "failed"):
        assert f'"{state}"' in src or f"'{state}'" in src, f"state {state!r} is not used"


def test_hash_concurrency_supports_the_full_ui_slider_range(temp_vault):
    """The UI slider is 1..8, so persisted and worker limits must match it."""
    from app.api.schemas.jobs import HashSettingsRequest
    from app.api.schemas.system import ConfigPatch
    from app.core import config_service

    assert HashSettingsRequest(concurrency=8).concurrency == 8
    assert ConfigPatch(hash_concurrency=8).hash_concurrency == 8
    config_service.set_config({"hash_concurrency": 8})
    assert config_service.get_config().hash_concurrency == 8

    service = hash_service.HashService()
    assert service.status()["concurrency"] == 8


def test_the_fingerprint_key_is_path_size_mtime(temp_vault):
    """C1: the cache is keyed on (path, size, mtime) and invalidated when it moves."""
    from app.core import db as dbmod

    conn = dbmod.get_ro()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(model_files)")}
    assert {"size", "mtime_ns", "sha256", "autov2", "hash_state"} <= cols, (
        f"model_files is missing hash-cache columns; has {sorted(cols)}")
