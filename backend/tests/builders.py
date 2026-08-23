"""File builders shared across the suite.

Kept out of ``conftest.py`` so any test module can import them directly; with
``--import-mode=importlib`` a conftest is not importable under its own name.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

MODEL_DIRS = ("checkpoints", "loras", "vae", "diffusion_models", "text_encoders",
              "controlnet", "upscale_models", "embeddings")


def write_safetensors(path: Path, tensors: dict, metadata: dict | None = None) -> Path:
    """Write a real, parseable safetensors file.

    ``tensors`` maps name -> (dtype, shape).  Bodies are zero-filled: the parsers
    under test only ever read the header, and a zeroed body keeps fixtures small
    while still being a byte-accurate container.
    """
    itemsize = {"F32": 4, "F16": 2, "BF16": 2, "F8_E4M3": 1, "I64": 8, "I32": 4, "U8": 1}
    header: dict = {}
    offset = 0
    for name, (dtype, shape) in tensors.items():
        n = 1
        for d in shape:
            n *= d
        nbytes = n * itemsize.get(dtype, 4)
        header[name] = {"dtype": dtype, "shape": list(shape),
                        "data_offsets": [offset, offset + nbytes]}
        offset += nbytes
    if metadata:
        header["__metadata__"] = {k: str(v) for k, v in metadata.items()}
    blob = json.dumps(header).encode()
    pad = (8 - (len(blob) % 8)) % 8
    blob += b" " * pad
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<Q", len(blob)))
        fh.write(blob)
        fh.write(b"\0" * offset)
    return path


def write_png_with_prompt(path: Path, prompt: dict, workflow: dict | None = None) -> Path:
    """A minimal but genuine PNG carrying ComfyUI's tEXt chunks."""
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x7f" * 12 for _ in range(4))
    out = [b"\x89PNG\r\n\x1a\n", chunk(b"IHDR", ihdr)]
    out.append(chunk(b"tEXt", b"prompt\x00" + json.dumps(prompt).encode()))
    if workflow is not None:
        out.append(chunk(b"tEXt", b"workflow\x00" + json.dumps(workflow).encode()))
    out.append(chunk(b"IDAT", zlib.compress(raw)))
    out.append(chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(out))
    return path


