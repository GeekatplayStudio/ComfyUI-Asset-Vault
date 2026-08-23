"""Fixtures for the security suite.

Everything here is hermetic: a synthetic ComfyUI tree in ``tmp_path`` and a
freshly migrated ``vault.db``.  **No security test ever writes into the owner's
real library** - the 1.5 TB model store and the 3,834 indexed outputs must be
byte-identical before and after a run.  The only assertions that read the real
install are marked ``live`` and are read-only.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

REPO = BACKEND.parent
APP = BACKEND / "app"
DOCS = REPO / "docs"

MODEL_DIRS = ("checkpoints", "loras", "vae", "controlnet", "clip", "unet",
              "diffusion_models", "text_encoders", "embeddings", "upscale_models")


def write_safetensors(path: Path, tensors: dict, metadata: dict | None = None) -> Path:
    """A structurally valid safetensors file with a real 8-byte header length."""
    header: dict = {}
    offset = 0
    for name, (dtype, shape) in tensors.items():
        width = {"F16": 2, "F32": 4, "BF16": 2, "F8_E4M3": 1}.get(dtype, 4)
        size = width
        for dim in shape:
            size *= dim
        header[name] = {"dtype": dtype, "shape": list(shape),
                        "data_offsets": [offset, offset + size]}
        offset += size
    if metadata:
        header["__metadata__"] = {str(k): str(v) for k, v in metadata.items()}
    blob = json.dumps(header).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<Q", len(blob)))
        fh.write(blob)
        fh.write(b"\0" * min(offset, 4096))
    return path


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO


@pytest.fixture(scope="session")
def app_dir() -> Path:
    return APP


@pytest.fixture(scope="session")
def docs_dir() -> Path:
    return DOCS


@pytest.fixture
def sec_comfyui(tmp_path: Path) -> Path:
    """A miniature but structurally real ComfyUI install."""
    root = tmp_path / "ComfyUI"
    for d in MODEL_DIRS:
        (root / "models" / d).mkdir(parents=True, exist_ok=True)
    for d in ("output", "input", "custom_nodes", "workflows"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "user" / "default" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("# entry point\n", encoding="utf-8")
    (root / "comfyui_version.py").write_text('__version__ = "0.33.0"\n',
                                             encoding="utf-8")
    write_safetensors(
        root / "models" / "checkpoints" / "probe-ckpt.safetensors",
        {"model.diffusion_model.input_blocks.0.0.weight": ("F16", (320, 4, 3, 3)),
         "first_stage_model.encoder.conv_in.weight": ("F16", (128, 3, 3, 3))},
    )
    write_safetensors(
        root / "models" / "loras" / "probe-lora.safetensors",
        {"lora_unet_down_blocks_0_attentions_0.lora_down.weight": ("F16", (32, 320)),
         "lora_unet_down_blocks_0_attentions_0.lora_up.weight": ("F16", (320, 32))},
    )
    (root / "user" / "default" / "workflows" / "probe.json").write_text(
        json.dumps({"nodes": [{"id": 1, "type": "CheckpointLoaderSimple",
                               "widgets_values": ["probe-ckpt.safetensors"]}],
                    "links": []}), encoding="utf-8")
    return root


@pytest.fixture
def sec_vault(tmp_path: Path, sec_comfyui: Path):
    """A migrated, empty vault DB wired to the synthetic install."""
    from app.core import config_service
    from app.core import db as dbmod
    from app.core.migrations import migrate

    prev = dbmod.db_path()
    dbmod.set_db_path(tmp_path / "vault.db")
    config_service.invalidate()
    migrate()
    cfg = config_service.set_config({"comfyui_path": str(sec_comfyui),
                                     "is_configured": True})
    try:
        yield cfg
    finally:
        dbmod.shutdown_writer()
        dbmod.close_thread_connections()
        dbmod.set_db_path(prev)
        config_service.invalidate()


@pytest.fixture
def client(sec_vault):
    """FastAPI TestClient bound to the synthetic vault."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        c.headers.update({"X-Vault-Request": "1"})
        yield c


@pytest.fixture
def naked_client(sec_vault):
    """TestClient that does *not* send the CSRF header."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def indexed_client(client, sec_comfyui):
    """A client whose vault has been scanned, so real uids exist."""
    import time as _t

    from app.indexing.service import get_indexer

    indexer = get_indexer()
    indexer.start(mode="full", force=True, enrich_online=False, trigger="test")
    deadline = _t.monotonic() + 60
    while _t.monotonic() < deadline:
        if not indexer.status().get("running"):
            break
        _t.sleep(0.05)
    return client


def python_sources(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
