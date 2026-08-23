"""Safe key extraction from ``.pt``/``.pth``/``.ckpt``/``.bin`` checkpoints.

**NEVER unpickles.**  ``pickletools.genops`` only *disassembles* the opcode
stream - it never executes a ``REDUCE`` or ``GLOBAL``.  That is the security
boundary (ARCHITECTURE 4.3.2).
"""

from __future__ import annotations

import io
import os
import pickletools
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..core import errors
from ..core.pathsafe import long_path

MAX_PICKLE = 32 * 1024 * 1024
MAX_STRINGS = 200_000
_STRING_OPS = {"SHORT_BINUNICODE", "BINUNICODE", "UNICODE", "BINUNICODE8",
               "SHORT_BINSTRING", "BINSTRING", "STRING"}

_KEYISH = (".weight", ".bias", ".alpha", ".gamma", ".beta", ".running_mean",
           ".running_var", ".scale", ".shift", ".embedding", ".proj", ".norm",
           "_weight", "_bias")


@dataclass
class TorchResult:
    ok: bool = False
    integrity: str = "ok"
    integrity_note: str | None = None
    error_code: str | None = None
    fmt: str = "torch_legacy"
    keys: list[str] = field(default_factory=list)
    strings: list[str] = field(default_factory=list)
    tensor_count: int = 0
    file_size: int = 0


def _looks_like_tensor_key(s: str) -> bool:
    if not s or len(s) > 300 or "\n" in s:
        return False
    if s.endswith(_KEYISH):
        return True
    # dotted paths with a numeric block index are almost always tensor keys
    return bool("." in s and any(part.isdigit() for part in s.split(".")))


def read_keys(path: str | Path, *, file_size: int | None = None) -> TorchResult:
    res = TorchResult()
    p = long_path(path)
    try:
        res.file_size = file_size if file_size is not None else os.path.getsize(p)
    except OSError as exc:
        res.integrity = "unreadable"
        res.error_code = errors.classify_os_error(exc)
        return res

    if not zipfile.is_zipfile(p):
        res.fmt = "torch_legacy"
        res.integrity = "unsupported_format"
        res.integrity_note = "Legacy pickle container; keys are not readable without unpickling."
        return res

    res.fmt = "torch_zip"
    try:
        with zipfile.ZipFile(p) as zf:
            names = zf.namelist()
            pkl = [n for n in names if n.endswith("data.pkl")]
            if not pkl:
                pkl = [n for n in names if n.endswith(".pkl")]
            if not pkl:
                res.integrity = "unsupported_format"
                res.integrity_note = "ZIP container has no data.pkl."
                return res
            info = zf.getinfo(pkl[0])
            if info.file_size > MAX_PICKLE:
                res.integrity = "unsupported_format"
                res.integrity_note = f"data.pkl is {info.file_size} bytes; refusing to scan."
                return res
            blob = zf.read(pkl[0])
            data_files = {n.rsplit("/", 1)[-1] for n in names if "/data/" in n}
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        res.integrity = "unreadable" if isinstance(exc, OSError) else "invalid_header"
        res.error_code = errors.classify_os_error(exc)
        res.integrity_note = str(exc)[:300]
        return res

    strings: list[str] = []
    try:
        for opcode, arg, _pos in pickletools.genops(io.BytesIO(blob)):
            if opcode.name in _STRING_OPS and isinstance(arg, (str, bytes)):
                s = arg.decode("utf-8", "replace") if isinstance(arg, bytes) else arg
                strings.append(s)
                if len(strings) >= MAX_STRINGS:
                    break
    except Exception as exc:  # noqa: BLE001 - malformed pickles are data, not bugs
        res.integrity_note = f"Pickle disassembly stopped early: {exc}"[:300]

    res.strings = strings
    seen: set[str] = set()
    for s in strings:
        if s in seen:
            continue
        if _looks_like_tensor_key(s):
            seen.add(s)
            res.keys.append(s)
    res.tensor_count = len(res.keys) or len(data_files)
    res.ok = bool(res.keys)
    if not res.ok:
        res.integrity = "unsupported_format"
        res.integrity_note = res.integrity_note or "No tensor-shaped keys recovered."
    return res


def read_onnx_producer(path: str | Path) -> dict:
    """Read ``producer_name``/``graph.name`` from an ONNX file via a varint scan."""
    p = long_path(path)
    out: dict = {}
    try:
        with open(p, "rb") as fh:
            blob = fh.read(65536)
    except OSError:
        return out
    i = 0
    n = len(blob)
    while i < n:
        try:
            tag = blob[i]
            field_no, wire = tag >> 3, tag & 0x07
            i += 1
            if wire == 2:
                length = 0
                shift = 0
                while i < n:
                    b = blob[i]
                    i += 1
                    length |= (b & 0x7F) << shift
                    if not b & 0x80:
                        break
                    shift += 7
                    if shift > 28:
                        return out
                if length < 0 or i + length > n or length > 4096:
                    return out
                val = blob[i:i + length]
                i += length
                if field_no == 2:
                    out.setdefault("producer_name", val.decode("utf-8", "replace"))
                elif field_no == 3:
                    out.setdefault("producer_version", val.decode("utf-8", "replace"))
                elif field_no == 4:
                    out.setdefault("domain", val.decode("utf-8", "replace"))
            elif wire == 0:
                while i < n and blob[i] & 0x80:
                    i += 1
                i += 1
            elif wire == 5:
                i += 4
            elif wire == 1:
                i += 8
            else:
                return out
        except (IndexError, UnicodeDecodeError):
            return out
        if len(out) >= 3:
            break
    return out


def is_zip(path: str | Path) -> bool:
    try:
        return zipfile.is_zipfile(long_path(path))
    except OSError:
        return False


def _unused(_: Path) -> None:  # pragma: no cover
    return None
