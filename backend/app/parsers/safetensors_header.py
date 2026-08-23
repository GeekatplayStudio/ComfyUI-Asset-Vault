"""Safetensors header reader.

Reads the FULL tensor key set - never a truncated window - plus the verbatim
``__metadata__`` block.  Includes the Layer-0 integrity checks from
ARCHITECTURE 4.3 that catch HTML error pages saved as ``.safetensors``.
"""

from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

from ..core import errors
from ..core.pathsafe import long_path

MAX_HEADER = 200 * 1024 * 1024
MIN_FILE = 4096
METADATA_CAP = 64 * 1024

_HTML_SNIFF = (b"<!doctype", b"<html", b"<?xml", b"version http", b"{\n", b"<!DOCTYPE")

DTYPE_BITS = {
    "F64": 64, "I64": 64, "U64": 64,
    "F32": 32, "I32": 32, "U32": 32,
    "F16": 16, "BF16": 16, "I16": 16, "U16": 16,
    "F8_E4M3": 8, "F8_E5M2": 8, "I8": 8, "U8": 8, "BOOL": 8,
    "F4": 4, "NF4": 4, "E8M0": 8,
}

PRECISION_BY_DTYPE = {
    "F64": "fp64", "F32": "fp32", "F16": "fp16", "BF16": "bf16",
    "F8_E4M3": "fp8", "F8_E5M2": "fp8", "I8": "int8", "U8": "uint8",
    "I64": "int64", "I32": "int32", "I16": "int16", "BOOL": "bool",
    "F4": "fp4", "NF4": "nf4",
}


@dataclass
class HeaderResult:
    ok: bool = False
    integrity: str = "ok"
    integrity_note: str | None = None
    error_code: str | None = None
    keys: list[str] = field(default_factory=list)
    shapes: dict[str, list[int]] = field(default_factory=dict)
    dtypes: dict[str, str] = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    tensor_count: int = 0
    param_total: int = 0
    header_bytes: int = 0
    file_size: int = 0

    def dtype_of(self, key: str) -> str | None:
        return self.dtypes.get(key)


def _numel(shape) -> int:
    n = 1
    if not isinstance(shape, (list, tuple)):
        return 0
    for d in shape:
        if not isinstance(d, int) or d < 0:
            return 0
        n *= d
    return n


def read_header(path: str | Path, *, file_size: int | None = None) -> HeaderResult:
    """Parse a safetensors header.  Never raises for file-level problems."""
    res = HeaderResult()
    p = long_path(path)
    try:
        size = file_size if file_size is not None else os.path.getsize(p)
    except OSError as exc:
        res.integrity = "unreadable"
        res.error_code = errors.classify_os_error(exc)
        res.integrity_note = str(exc)[:300]
        return res
    res.file_size = size

    if size < MIN_FILE:
        res.integrity = "truncated"
        res.error_code = errors.NOT_A_MODEL
        res.integrity_note = f"File is only {size} bytes."
        return res

    try:
        with open(p, "rb") as fh:
            lead = fh.read(32)
            if len(lead) < 8:
                res.integrity = "truncated"
                res.error_code = errors.HEADER_INVALID
                return res
            head8 = lead[:8]
            sniff = lead.lower()
            if any(sniff.startswith(s.lower()) for s in _HTML_SNIFF):
                res.integrity = "not_a_model"
                res.error_code = errors.NOT_A_MODEL
                res.integrity_note = "File begins with markup or a Git-LFS pointer, not a safetensors header."
                return res
            fh.seek(8)
            header_len = struct.unpack("<Q", head8)[0]
            res.header_bytes = header_len
            if header_len < 2 or header_len > min(MAX_HEADER, max(0, size - 8)):
                res.integrity = "invalid_header"
                res.error_code = (errors.HEADER_TOO_LARGE if header_len > MAX_HEADER
                                  else errors.HEADER_INVALID)
                res.integrity_note = (
                    f"Declared header length {header_len} is impossible for a "
                    f"{size}-byte file."
                )
                return res
            raw = fh.read(header_len)
    except OSError as exc:
        res.integrity = "unreadable"
        res.error_code = errors.classify_os_error(exc)
        res.integrity_note = str(exc)[:300]
        return res

    if len(raw) < header_len:
        res.integrity = "truncated"
        res.error_code = errors.HEADER_INVALID
        res.integrity_note = "Header is shorter than declared."
        return res

    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        res.integrity = "invalid_header"
        res.error_code = (errors.ENCODING_ERROR if isinstance(exc, UnicodeDecodeError)
                          else errors.HEADER_INVALID)
        res.integrity_note = str(exc)[:300]
        return res

    if not isinstance(header, dict):
        res.integrity = "invalid_header"
        res.error_code = errors.HEADER_INVALID
        return res

    md = header.get("__metadata__")
    res.metadata = md if isinstance(md, dict) else {}
    total = 0
    for key, spec in header.items():
        if key == "__metadata__":
            continue
        res.keys.append(key)
        if isinstance(spec, dict):
            shape = spec.get("shape")
            dtype = spec.get("dtype")
            if isinstance(shape, list):
                res.shapes[key] = [d for d in shape if isinstance(d, int)]
                total += _numel(shape)
            if isinstance(dtype, str):
                res.dtypes[key] = dtype
    res.tensor_count = len(res.keys)
    res.param_total = total
    res.ok = True
    if not res.keys:
        res.integrity = "invalid_header"
        res.error_code = errors.HEADER_INVALID
        res.integrity_note = "Header declares no tensors."
        res.ok = False
    return res


def metadata_json(md: dict) -> str | None:
    """Serialize ``__metadata__`` verbatim, capped at 64 KB."""
    if not md:
        return None
    try:
        s = json.dumps(md, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    if len(s) <= METADATA_CAP:
        return s
    trimmed = {}
    budget = METADATA_CAP
    for k, v in md.items():
        sv = str(v)
        if len(sv) > 2000:
            sv = sv[:2000] + "…"
        budget -= len(k) + len(sv) + 6
        if budget <= 0:
            trimmed["__truncated__"] = True
            break
        trimmed[k] = sv
    try:
        return json.dumps(trimmed, ensure_ascii=False, default=str)[:METADATA_CAP]
    except (TypeError, ValueError):
        return None


def dominant_precision(dtypes: dict[str, str], shapes: dict[str, list[int]]) -> tuple[str | None, str | None]:
    """Return (precision, quantization-hint) weighted by parameter count."""
    if not dtypes:
        return None, None
    weight: dict[str, int] = {}
    for key, dt in dtypes.items():
        n = _numel(shapes.get(key, []))
        weight[dt] = weight.get(dt, 0) + max(1, n)
    # Ignore tiny scalar/index tensors when choosing the headline precision.
    ranked = sorted(weight.items(), key=lambda kv: -kv[1])
    top = ranked[0][0]
    precision = PRECISION_BY_DTYPE.get(top, top.lower())
    distinct = {PRECISION_BY_DTYPE.get(d, d.lower()) for d in weight}
    if len(distinct) > 1 and precision in ("fp8", "int8", "fp4", "nf4"):
        precision = precision  # a quantized body with fp16 norms is still fp8
    elif len(distinct) > 2:
        precision = "mixed"
    quant = None
    if top in ("F8_E4M3", "F8_E5M2"):
        quant = "fp8_" + ("e4m3" if top == "F8_E4M3" else "e5m2")
    elif top in ("I8", "U8"):
        quant = "int8"
    elif top in ("F4", "NF4"):
        quant = top.lower()
    return precision, quant
