"""Minimal pure-stdlib MP4/MOV box parser: duration and track dimensions.

Used for video output metadata.  There is deliberately no frame extraction
(DECISIONS D6) - that would need ffmpeg or PyAV.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from ..core.pathsafe import long_path

MAX_SCAN = 64 * 1024 * 1024
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta"}


@dataclass
class Mp4Info:
    ok: bool = False
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    brand: str | None = None
    frame_count: int | None = None


def _read_boxes(fh, end: int, info: Mp4Info, depth: int = 0) -> None:
    if depth > 6:
        return
    while fh.tell() < end:
        pos = fh.tell()
        header = fh.read(8)
        if len(header) < 8:
            return
        size, kind = struct.unpack(">I4s", header)
        if size == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            body_start = pos + 16
        elif size == 0:
            size = end - pos
            body_start = pos + 8
        else:
            body_start = pos + 8
        if size < 8 or pos + size > end:
            return
        box_end = pos + size

        if kind == b"ftyp":
            fh.seek(body_start)
            brand = fh.read(4)
            info.brand = brand.decode("ascii", "replace").strip()
        elif kind in _CONTAINERS:
            fh.seek(body_start)
            _read_boxes(fh, box_end, info, depth + 1)
        elif kind == b"mvhd":
            fh.seek(body_start)
            ver = fh.read(4)
            if len(ver) < 4:
                return
            if ver[0] == 1:
                blob = fh.read(28)
                if len(blob) >= 28:
                    timescale, duration = struct.unpack(">IQ", blob[16:28])
                else:
                    timescale = duration = 0
            else:
                blob = fh.read(16)
                if len(blob) >= 16:
                    timescale, duration = struct.unpack(">II", blob[8:16])
                else:
                    timescale = duration = 0
            if timescale:
                info.duration_ms = int(duration * 1000 / timescale)
                info.ok = True
        elif kind == b"tkhd":
            fh.seek(body_start)
            ver = fh.read(4)
            if len(ver) < 4:
                return
            skip = 32 if ver[0] == 1 else 20
            fh.read(skip)
            fh.read(52 - 8)  # reserved + layer + volume + matrix
            blob = fh.read(8)
            if len(blob) >= 8:
                w, h = struct.unpack(">II", blob)
                w >>= 16
                h >>= 16
                if 0 < w < 20000 and 0 < h < 20000:
                    info.width = info.width or w
                    info.height = info.height or h
                    info.ok = True
        fh.seek(box_end)


def read_info(path: str | Path) -> Mp4Info:
    info = Mp4Info()
    try:
        p = long_path(path)
        with open(p, "rb") as fh:
            fh.seek(0, 2)
            end = min(fh.tell(), MAX_SCAN)
            fh.seek(0)
            _read_boxes(fh, end, info)
    except (OSError, struct.error, ValueError):
        return info
    return info
