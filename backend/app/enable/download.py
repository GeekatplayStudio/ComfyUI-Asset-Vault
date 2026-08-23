"""The model fetcher: verify before placing, quarantine on mismatch.

Covers SECURITY_REVIEW R2 (redirects re-validated), R3 (destination derived),
R4 (verify then place), R5 (never overwrite implicitly), R6 (free space) and the
resumable half of R11.

Shape of one fetch, in order, because the order *is* the safety property:

1. Destination derived and proven inside a configured root - **before a socket
   is opened**.
2. Free space checked against the advertised size plus a 5% margin.
3. Bytes streamed to ``<target>.part``, one redirect hop at a time, each hop
   re-validated against the allowlist and any ``Authorization`` dropped on a
   host change.  Free space re-checked every 256 MB.
4. The finished ``.part`` is hashed **from disk, in full** - a resumed download
   never trusts the prefix it already had.
5. Size and, when the source published one, SHA-256 are compared.  Only a match
   reaches ``os.replace``.  A mismatch is moved to ``<root>/.vault-quarantine/``
   with a reason file and reported as ``INTEGRITY_MISMATCH``.

There is no ``overwrite`` mode.  There is no way to name a destination.  There
is no ``subprocess``, ``exec``, ``eval`` or ``importlib`` in this module.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..config import QUARANTINE_DIRNAME
from ..core import db as dbmod
from ..core.errors import (
    ConflictError,
    InsufficientSpace,
    IntegrityMismatch,
    UpstreamUnavailable,
    ValidationError,
    classify_os_error,
)
from ..core.pathsafe import long_path, normalize
from ..jobs.hash_service import compute_sha256
from . import hosts

log = logging.getLogger(__name__)

CHUNK = 1 << 20                       # 1 MB off the wire
SPACE_RECHECK_BYTES = 256 * 1024 * 1024
SPACE_MARGIN = 1.05                   # R6: advertised size + 5%
CONNECT_TIMEOUT_S = 20.0
READ_TIMEOUT_S = 120.0
MAX_UNSIZED_BYTES = 64 * 1024 * 1024 * 1024   # a source that declares nothing
USER_AGENT = "GeekatplayAssetVault/2.0"

ON_CONFLICT = ("fail", "skip", "keep_both")


class Cancelled(Exception):
    """The user stopped this fetch.  Leaves the ``.part`` file for resume."""


@dataclass
class FetchResult:
    state: str                        # done | skipped | quarantined | failed | cancelled
    abs_path: str | None = None
    bytes_written: int = 0
    sha256: str | None = None
    verified: str = "none"            # sha256 | size | none
    quarantine_path: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "state": self.state, "abs_path": self.abs_path,
            "bytes": self.bytes_written, "sha256": self.sha256,
            "verified": self.verified, "quarantine_path": self.quarantine_path,
            "error_code": self.error_code, "error_message": self.error_message,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# R6 - free space
# ---------------------------------------------------------------------------

def disk_free(path: str | Path) -> tuple[int, int]:
    """``(free, total)`` for the volume holding ``path``.  Walks up if needed."""
    probe = Path(str(path))
    for _ in range(6):
        try:
            usage = shutil.disk_usage(str(probe))
        except OSError:
            parent = probe.parent
            if parent == probe:
                break
            probe = parent
            continue
        return int(usage.free), int(usage.total)
    return 0, 0


def required_with_margin(size: int) -> int:
    return int(max(0, int(size)) * SPACE_MARGIN)


def check_space(directory: str | Path, needed: int, *,
                items: int = 1) -> dict:
    """Refuse before starting when the volume cannot hold the request (R6)."""
    free, total = disk_free(directory)
    required = required_with_margin(needed)
    if needed > 0 and required > free:
        raise InsufficientSpace(
            "Not enough free space on the target drive for this download.",
            details={
                "directory": str(directory), "items": int(items),
                "required_bytes": required, "download_bytes": int(needed),
                "margin_pct": int((SPACE_MARGIN - 1) * 100),
                "free_bytes": free, "total_bytes": total,
                "shortfall_bytes": max(0, required - free),
            },
        )
    return {"free_bytes": free, "total_bytes": total, "required_bytes": required}


# ---------------------------------------------------------------------------
# R5 - conflicts
# ---------------------------------------------------------------------------

def _unique_sibling(target: str) -> str:
    stem, ext = os.path.splitext(target)
    for n in range(2, 500):
        candidate = f"{stem} ({n}){ext}"
        if not os.path.exists(long_path(candidate)):
            return candidate
    raise ConflictError("Could not find a free name beside the existing file.",
                        details={"path": target})


def resolve_conflict(target: str, on_conflict: str) -> tuple[str, str | None]:
    """``(effective target, action)``.  ``overwrite`` is not offered at all."""
    mode = str(on_conflict or "fail").lower()
    if mode not in ON_CONFLICT:
        raise ValidationError(
            f"on_conflict must be one of {'|'.join(ON_CONFLICT)}; "
            "this endpoint never overwrites an existing model.",
            details={"on_conflict": on_conflict, "allowed": list(ON_CONFLICT)})
    if not os.path.exists(long_path(target)):
        return target, None
    if mode == "skip":
        return target, "skip"
    if mode == "keep_both":
        return _unique_sibling(target), "keep_both"
    raise ConflictError(
        "A file already exists at the destination and this app never overwrites "
        "a model implicitly. Choose keep_both or skip.",
        details={"path": target, "size": _size_of(target),
                 "allowed": ["skip", "keep_both"]})


def _size_of(path: str) -> int:
    try:
        return int(os.path.getsize(long_path(path)))
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# R4 - quarantine
# ---------------------------------------------------------------------------

def quarantine_dir(root_path: str | Path) -> Path:
    return Path(str(normalize(root_path))) / QUARANTINE_DIRNAME


def quarantine(part_path: str, *, root_path: str, reason: dict) -> str:
    """Park a file that failed verification, with the evidence beside it."""
    slot = quarantine_dir(root_path) / (
        f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}")
    slot.mkdir(parents=True, exist_ok=True)
    name = os.path.basename(part_path) or "download.part"
    target = slot / name
    try:
        shutil.move(long_path(part_path), long_path(str(target)))
    except OSError as exc:
        log.warning("could not move %s into quarantine: %s", part_path, exc)
        with contextlib.suppress(OSError):
            os.replace(long_path(part_path), long_path(str(target)))
    payload = {"quarantined_at": dbmod.now_ms(), "original_part": part_path, **reason}
    with contextlib.suppress(OSError, TypeError, ValueError):
        (slot / "reason.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8")
    return str(target)


def quarantine_list(cfg=None) -> list[dict]:
    """Everything currently parked, so the UI can show it rather than hide it."""
    from ..core import config_service

    cfg = cfg or config_service.get_config()
    out: list[dict] = []
    seen: set[str] = set()
    for root in cfg.roots:
        base = quarantine_dir(root.path)
        key = str(base).lower()
        if key in seen or not base.is_dir():
            continue
        seen.add(key)
        try:
            slots = sorted(os.scandir(long_path(str(base))), key=lambda e: e.name,
                           reverse=True)
        except OSError:
            continue
        for slot in slots:
            if not slot.is_dir():
                continue
            entry: dict = {"id": slot.name, "root_id": root.id, "abs_path": slot.path,
                           "files": [], "bytes": 0, "reason": None}
            reason_file = Path(slot.path) / "reason.json"
            try:
                entry["reason"] = json.loads(reason_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                entry["reason"] = None
            try:
                for f in os.scandir(long_path(slot.path)):
                    if f.name == "reason.json" or not f.is_file():
                        continue
                    size = f.stat().st_size
                    entry["files"].append({"name": f.name, "size": int(size)})
                    entry["bytes"] += int(size)
            except OSError:
                pass
            out.append(entry)
    return out


# ---------------------------------------------------------------------------
# The fetch itself
# ---------------------------------------------------------------------------

@dataclass
class FetchSpec:
    """Everything one download needs.  Every field is server-derived."""

    url: str
    host: str
    target_abs_path: str
    root_path: str
    expected_size: int = 0
    expected_sha256: str | None = None
    on_conflict: str = "fail"
    ref_name: str = ""
    category: str = ""


def _client(timeout):
    import httpx

    # R2: redirects are never followed by the library.  Every hop is checked
    # here first, which is the whole lesson of S-18.
    return httpx.Client(follow_redirects=False, timeout=timeout,
                        headers={"User-Agent": USER_AGENT})


def _open_stream(client, checked: hosts.CheckedUrl, headers: dict):
    """Walk the redirect chain by hand, validating each hop before taking it."""
    import httpx

    current = checked
    current_headers = dict(headers)
    hop = 0
    while True:
        request = client.build_request("GET", current.url, headers=current_headers)
        try:
            response = client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(
                f"Could not reach {current.host}.",
                details={"host": current.host, "error": str(exc)[:200]}) from exc
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            response.close()
            nxt = hosts.check_redirect(location, current=current, hop=hop)
            current_headers = hosts.strip_auth_on_host_change(
                current_headers, current, nxt)
            current = nxt
            hop += 1
            continue
        return response, current


def fetch(spec: FetchSpec, *, cancel: threading.Event | None = None,
          on_progress=None) -> FetchResult:
    """Download one model file.  Never raises for an expected failure."""
    checked = hosts.check(spec.url, kind=hosts.KIND_MODEL)
    target, action = resolve_conflict(spec.target_abs_path, spec.on_conflict)
    if action == "skip":
        return FetchResult(state="skipped", abs_path=target,
                           notes=["a file already exists at the destination"])
    directory = os.path.dirname(target)
    part = target + ".part"

    try:
        os.makedirs(long_path(directory), exist_ok=True)
    except OSError as exc:
        return FetchResult(state="failed", error_code=classify_os_error(exc),
                           error_message=str(exc)[:300])

    resume_from = _size_of(part)
    if spec.expected_size and resume_from >= spec.expected_size:
        resume_from = 0
    need = max(0, int(spec.expected_size) - resume_from)
    check_space(directory, need or int(spec.expected_size))

    headers: dict[str, str] = {}
    if resume_from > 0:
        headers["Range"] = f"bytes={resume_from}-"

    try:
        with _client((CONNECT_TIMEOUT_S, READ_TIMEOUT_S)) as client:
            response, final = _open_stream(client, checked, headers)
            try:
                written, appended = _stream_to_part(
                    response, part, resume_from=resume_from, spec=spec,
                    directory=directory, cancel=cancel, on_progress=on_progress)
            finally:
                response.close()
    except Cancelled:
        return FetchResult(state="cancelled", bytes_written=_size_of(part),
                           notes=["the partial file was kept so the fetch can resume"])
    except (UpstreamUnavailable, InsufficientSpace) as exc:
        return FetchResult(state="failed", error_code=exc.code,
                           error_message=exc.message, bytes_written=_size_of(part))
    except OSError as exc:
        return FetchResult(state="failed", error_code=classify_os_error(exc),
                           error_message=str(exc)[:300], bytes_written=_size_of(part))
    except Exception as exc:  # noqa: BLE001 - a transport failure is reported, not raised
        return FetchResult(state="failed", error_code="UPSTREAM_UNAVAILABLE",
                           error_message=str(exc)[:300], bytes_written=_size_of(part))

    del appended, final
    return _verify_and_place(spec, part=part, target=target, written=written,
                             cancel=cancel)


def _stream_to_part(response, part: str, *, resume_from: int, spec: FetchSpec,
                    directory: str, cancel, on_progress) -> tuple[int, bool]:
    """Write the body to ``<target>.part``, honouring resume and R6 re-checks."""
    import httpx

    status = response.status_code
    if status not in (200, 206):
        with contextlib.suppress(httpx.HTTPError):
            response.read()
        raise UpstreamUnavailable(
            f"The source answered HTTP {status}.",
            details={"host": spec.host, "status": status})

    appended = status == 206 and resume_from > 0
    if resume_from > 0 and not appended:
        # The server ignored Range, so the prefix is worthless.  Start again
        # rather than concatenating two different byte streams.
        resume_from = 0

    declared = 0
    with contextlib.suppress(TypeError, ValueError):
        declared = int(response.headers.get("content-length") or 0)
    total = int(spec.expected_size) or (declared + resume_from)
    if not total and declared:
        total = declared
    if not spec.expected_size and total > MAX_UNSIZED_BYTES:
        raise UpstreamUnavailable(
            "The source advertises an implausible size.",
            details={"host": spec.host, "declared_bytes": total})

    mode = "ab" if appended else "wb"
    done = resume_from if appended else 0
    since_check = 0
    last_emit = 0.0
    with open(long_path(part), mode) as fh:
        for chunk in response.iter_bytes(CHUNK):
            if cancel is not None and cancel.is_set():
                fh.flush()
                raise Cancelled
            fh.write(chunk)
            done += len(chunk)
            since_check += len(chunk)
            if since_check >= SPACE_RECHECK_BYTES:
                since_check = 0
                remaining = max(0, total - done)
                free, _total_bytes = disk_free(directory)
                if remaining and free < remaining:
                    fh.flush()
                    raise InsufficientSpace(
                        "The target drive ran out of space during the download; "
                        "the partial file was kept so it can resume.",
                        details={"directory": directory, "free_bytes": free,
                                 "remaining_bytes": remaining})
            if on_progress is not None:
                now = time.monotonic()
                if now - last_emit >= 0.25:
                    last_emit = now
                    on_progress(done, total)
    if on_progress is not None:
        on_progress(done, total)
    return done, appended


def _verify_and_place(spec: FetchSpec, *, part: str, target: str, written: int,
                      cancel) -> FetchResult:
    """R4/R11: re-hash the whole file from disk, then place it or quarantine it."""
    actual_size = _size_of(part)
    digest, code, _read = compute_sha256(part, cancel=cancel)
    if code == "CANCELLED":
        return FetchResult(state="cancelled", bytes_written=actual_size)
    if digest is None:
        return FetchResult(state="failed", error_code=code or "UNKNOWN",
                           error_message="The downloaded file could not be read back.",
                           bytes_written=actual_size)

    problems: list[str] = []
    verified = "none"
    if spec.expected_size and actual_size != int(spec.expected_size):
        problems.append(
            f"size mismatch: expected {int(spec.expected_size)} bytes, got {actual_size}")
    elif spec.expected_size:
        verified = "size"
    if spec.expected_sha256:
        want = str(spec.expected_sha256).lower()
        if digest.lower() != want:
            problems.append(f"SHA-256 mismatch: expected {want}, got {digest}")
        else:
            verified = "sha256"
    if not spec.expected_size and not spec.expected_sha256:
        problems.append("the source published neither a size nor a hash")

    if problems:
        where = quarantine(part, root_path=spec.root_path, reason={
            "ref_name": spec.ref_name, "category": spec.category,
            "source_url": spec.url, "source_host": spec.host,
            "intended_path": target,
            "expected_size": int(spec.expected_size or 0),
            "expected_sha256": spec.expected_sha256,
            "actual_size": actual_size, "actual_sha256": digest,
            "problems": problems,
        })
        return FetchResult(
            state="quarantined", bytes_written=actual_size, sha256=digest,
            quarantine_path=where, error_code=IntegrityMismatch.code,
            error_message="; ".join(problems),
            notes=["nothing was written to the model folder"])

    if os.path.exists(long_path(target)):
        return FetchResult(
            state="failed", bytes_written=actual_size, sha256=digest,
            error_code=ConflictError.code,
            error_message="Another file appeared at the destination while the "
                          "download was running; the partial file was kept.",
            notes=[part])
    try:
        os.replace(long_path(part), long_path(target))
    except OSError as exc:
        return FetchResult(state="failed", bytes_written=actual_size, sha256=digest,
                           error_code=classify_os_error(exc),
                           error_message=str(exc)[:300])
    del written
    return FetchResult(state="done", abs_path=target, bytes_written=actual_size,
                       sha256=digest, verified=verified)
