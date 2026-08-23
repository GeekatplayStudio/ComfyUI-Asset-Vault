"""Minimal, dependency-free GGUF header reader (ARCHITECTURE 4.3.2)."""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

from ..core import errors
from ..core.pathsafe import long_path

MAGIC = b"GGUF"
MAX_KV = 4096
MAX_TENSORS = 200_000
MAX_STRING = 1 << 20

# GGUF metadata value types
(T_UINT8, T_INT8, T_UINT16, T_INT16, T_UINT32, T_INT32, T_FLOAT32, T_BOOL,
 T_STRING, T_ARRAY, T_UINT64, T_INT64, T_FLOAT64) = range(13)

_FIXED = {
    T_UINT8: ("<B", 1), T_INT8: ("<b", 1), T_UINT16: ("<H", 2), T_INT16: ("<h", 2),
    T_UINT32: ("<I", 4), T_INT32: ("<i", 4), T_FLOAT32: ("<f", 4), T_BOOL: ("<?", 1),
    T_UINT64: ("<Q", 8), T_INT64: ("<q", 8), T_FLOAT64: ("<d", 8),
}

GGML_TYPE_NAMES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 6: "Q5_0", 7: "Q5_1", 8: "Q8_0",
    9: "Q8_1", 10: "Q2_K", 11: "Q3_K", 12: "Q4_K", 13: "Q5_K", 14: "Q6_K",
    15: "Q8_K", 16: "IQ2_XXS", 17: "IQ2_XS", 18: "IQ3_XXS", 19: "IQ1_S",
    20: "IQ4_NL", 21: "IQ3_S", 22: "IQ2_S", 23: "IQ4_XS", 24: "I8", 25: "I16",
    26: "I32", 27: "I64", 28: "F64", 29: "IQ1_M", 30: "BF16",
}


@dataclass
class GgufResult:
    ok: bool = False
    integrity: str = "ok"
    integrity_note: str | None = None
    error_code: str | None = None
    version: int = 0
    metadata: dict = field(default_factory=dict)
    keys: list[str] = field(default_factory=list)
    shapes: dict[str, list[int]] = field(default_factory=dict)
    dtypes: dict[str, str] = field(default_factory=dict)
    tensor_count: int = 0
    param_total: int = 0
    file_size: int = 0


class _Reader:
    __slots__ = ("fh",)

    def __init__(self, fh) -> None:
        self.fh = fh

    def raw(self, n: int) -> bytes:
        b = self.fh.read(n)
        if len(b) < n:
            raise ValueError("unexpected end of GGUF header")
        return b

    def u32(self) -> int:
        return struct.unpack("<I", self.raw(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.raw(8))[0]

    def string(self) -> str:
        n = self.u64()
        if n > MAX_STRING:
            raise ValueError("GGUF string too long")
        return self.raw(n).decode("utf-8", "replace")

    def value(self, vtype: int, depth: int = 0):
        if vtype in _FIXED:
            fmt, size = _FIXED[vtype]
            return struct.unpack(fmt, self.raw(size))[0]
        if vtype == T_STRING:
            return self.string()
        if vtype == T_ARRAY:
            if depth > 2:
                raise ValueError("GGUF array nested too deep")
            itype = self.u32()
            count = self.u64()
            if count > 1_000_000:
                raise ValueError("GGUF array too long")
            out = []
            for _ in range(count):
                v = self.value(itype, depth + 1)
                if len(out) < 64:
                    out.append(v)
            return out
        raise ValueError(f"unknown GGUF value type {vtype}")


def read_header(path: str | Path, *, file_size: int | None = None) -> GgufResult:
    res = GgufResult()
    p = long_path(path)
    try:
        res.file_size = file_size if file_size is not None else os.path.getsize(p)
    except OSError as exc:
        res.integrity = "unreadable"
        res.error_code = errors.classify_os_error(exc)
        return res
    try:
        with open(p, "rb") as fh:
            r = _Reader(fh)
            if r.raw(4) != MAGIC:
                res.integrity = "not_a_model"
                res.error_code = errors.NOT_A_MODEL
                res.integrity_note = "Missing GGUF magic."
                return res
            res.version = r.u32()
            n_tensors = r.u64()
            n_kv = r.u64()
            if n_tensors > MAX_TENSORS or n_kv > MAX_KV:
                res.integrity = "invalid_header"
                res.error_code = errors.HEADER_INVALID
                res.integrity_note = f"Implausible counts: {n_tensors} tensors, {n_kv} kv."
                return res
            for _ in range(n_kv):
                key = r.string()
                vtype = r.u32()
                res.metadata[key] = r.value(vtype)
            total = 0
            for _ in range(n_tensors):
                name = r.string()
                ndim = r.u32()
                if ndim > 8:
                    raise ValueError("GGUF tensor rank too high")
                dims = [r.u64() for _ in range(ndim)]
                ggml_type = r.u32()
                r.u64()  # offset
                res.keys.append(name)
                res.shapes[name] = [int(d) for d in dims]
                res.dtypes[name] = GGML_TYPE_NAMES.get(ggml_type, f"GGML_{ggml_type}")
                n = 1
                for d in dims:
                    n *= int(d)
                total += n
            res.tensor_count = len(res.keys)
            res.param_total = total
            res.ok = True
    except (OSError, ValueError, struct.error) as exc:
        if isinstance(exc, OSError):
            res.integrity = "unreadable"
            res.error_code = errors.classify_os_error(exc)
        else:
            res.integrity = "invalid_header"
            res.error_code = errors.HEADER_INVALID
        res.integrity_note = str(exc)[:300]
        res.ok = bool(res.keys)
    return res


def quantization_label(md: dict) -> str | None:
    ft = md.get("general.file_type")
    if isinstance(ft, int):
        return f"gguf_ft{ft}"
    qv = md.get("general.quantization_version")
    return f"gguf_q{qv}" if isinstance(qv, int) else "gguf"
