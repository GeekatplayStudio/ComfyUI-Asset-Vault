"""Shared fixtures for the whole suite.

Two worlds live here.

*Hermetic* tests build a synthetic ComfyUI tree in ``tmp_path`` and a freshly
migrated ``vault.db``; they run anywhere, on any machine, with no ComfyUI.

*Live* tests read the owner's real install.  They are marked ``live`` and are
deselected by default (see ``pytest.ini``) so a clean checkout still goes green.
Every live fixture works on a **copy** of the vault DB and never writes into the
real library except through disposable probe files it creates and removes.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
for _p in (str(BACKEND), str(TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from builders import MODEL_DIRS, write_png_with_prompt, write_safetensors  # noqa: E402,F401

REPO = BACKEND.parent
DOCS = REPO / "docs"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The owner's install.  Overridable so the suite can be pointed elsewhere.
LIVE_ROOT = Path(os.environ.get("VAULT_LIVE_COMFYUI", r"C:\ComfyUI"))


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def backend_dir() -> Path:
    return BACKEND


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO


@pytest.fixture(scope="session")
def docs_dir() -> Path:
    return DOCS


@pytest.fixture(scope="session")
def live_root() -> Path:
    """The real ComfyUI install; skips when it is not mounted."""
    if not LIVE_ROOT.is_dir():
        pytest.skip(f"real ComfyUI install not present at {LIVE_ROOT}")
    return LIVE_ROOT


@pytest.fixture(scope="session")
def live_db(tmp_path_factory) -> Path:
    """A consistent, private snapshot of the indexed vault.

    ``shutil.copy`` would miss everything still sitting in the ``-wal`` file and
    hand back a stale database; the sqlite backup API takes a real snapshot.
    """
    from app.config import DB_PATH

    if not Path(DB_PATH).exists():
        pytest.skip("no indexed vault.db; run a scan first")
    target = tmp_path_factory.mktemp("live_vault") / "vault.db"
    src = sqlite3.connect(f"file:{Path(DB_PATH).as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


@pytest.fixture
def live_conn(live_db):
    """Read-only cursor onto the vault snapshot."""
    conn = sqlite3.connect(f"file:{live_db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Synthetic ComfyUI installation
# ---------------------------------------------------------------------------

@pytest.fixture
def synthetic_comfyui(tmp_path: Path) -> Path:
    """A miniature but structurally real ComfyUI install."""
    root = tmp_path / "ComfyUI"
    for d in MODEL_DIRS:
        (root / "models" / d).mkdir(parents=True, exist_ok=True)
    (root / "output").mkdir(parents=True, exist_ok=True)
    (root / "input").mkdir(parents=True, exist_ok=True)
    (root / "custom_nodes").mkdir(parents=True, exist_ok=True)
    (root / "user" / "default" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / "workflows").mkdir(parents=True, exist_ok=True)
    (root / "comfy").mkdir(parents=True, exist_ok=True)
    (root / "comfy_extras").mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("# ComfyUI entry point\n", encoding="utf-8")
    (root / "comfyui_version.py").write_text('__version__ = "0.33.0"\n', encoding="utf-8")

    write_safetensors(
        root / "models" / "checkpoints" / "sd15-probe.safetensors",
        {"model.diffusion_model.input_blocks.0.0.weight": ("F16", (320, 4, 3, 3)),
         "first_stage_model.encoder.conv_in.weight": ("F16", (128, 3, 3, 3)),
         "cond_stage_model.transformer.text_model.embeddings.token_embedding.weight":
             ("F16", (49408, 768))},
    )
    write_safetensors(
        root / "models" / "loras" / "probe-lora.safetensors",
        {"lora_unet_down_blocks_0_attentions_0.lora_down.weight": ("F16", (32, 320)),
         "lora_unet_down_blocks_0_attentions_0.lora_up.weight": ("F16", (320, 32))},
        metadata={"ss_base_model_version": "sd_v1", "ss_network_dim": "32"},
    )
    write_safetensors(
        root / "models" / "vae" / "probe-vae.safetensors",
        {"encoder.conv_in.weight": ("F32", (128, 3, 3, 3)),
         "decoder.conv_out.weight": ("F32", (3, 128, 3, 3))},
    )
    (root / "user" / "default" / "workflows" / "probe.json").write_text(
        json.dumps({"nodes": [
            {"id": 1, "type": "CheckpointLoaderSimple",
             "widgets_values": ["sd15-probe.safetensors"]},
            {"id": 2, "type": "KSampler", "widgets_values": [1, "fixed", 20, 7.0]},
            {"id": 3, "type": "AbsentThirdPartyNode", "widgets_values": []},
        ], "links": []}), encoding="utf-8")
    return root


@pytest.fixture
def temp_vault(tmp_path: Path, synthetic_comfyui: Path, monkeypatch):
    """A migrated, empty vault DB wired to the synthetic install.

    Yields the ``AppConfig``.  Restores global module state on teardown so tests
    never leak a DB path or a cached config into each other.
    """
    from app.core import config_service
    from app.core import db as dbmod
    from app.core.migrations import migrate

    prev_path = dbmod.db_path()
    db_file = tmp_path / "vault.db"
    dbmod.set_db_path(db_file)
    config_service.invalidate()
    migrate()
    cfg = config_service.set_config(
        {"comfyui_path": str(synthetic_comfyui), "is_configured": True}
    )
    try:
        yield cfg
    finally:
        dbmod.shutdown_writer()
        dbmod.close_thread_connections()
        dbmod.set_db_path(prev_path)
        config_service.invalidate()


@pytest.fixture
def hermetic_client(temp_vault):
    """FastAPI TestClient bound to the synthetic vault."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Disposable probes into the real library (live tests only)
# ---------------------------------------------------------------------------

class ProbeSet:
    """Tracks files created inside the real install so teardown is total.

    The owner's library must end every run at exactly the counts it started at,
    so nothing here ever touches a pre-existing file.
    """

    def __init__(self) -> None:
        self.paths: list[Path] = []

    def file(self, path: Path, data: bytes = b"probe") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.paths.append(path)
        return path

    def safetensors(self, path: Path, tensors: dict, metadata: dict | None = None) -> Path:
        write_safetensors(path, tensors, metadata)
        self.paths.append(path)
        return path

    def dir(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        self.paths.append(path)
        return path

    def cleanup(self) -> list[str]:
        failures = []
        for p in reversed(self.paths):
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                elif p.exists():
                    p.unlink()
            except OSError as exc:  # pragma: no cover - reported, never swallowed
                failures.append(f"{p}: {exc}")
        self.paths.clear()
        return failures


@pytest.fixture
def probes():
    ps = ProbeSet()
    try:
        yield ps
    finally:
        leftover = ps.cleanup()
        assert not leftover, f"probe cleanup failed, library not restored: {leftover}"


# ---------------------------------------------------------------------------
# Server availability for contract / MCP tests
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("VAULT_BASE_URL", "http://127.0.0.1:8127")


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def running_server(base_url: str) -> str:
    """Skips unless a backend is already answering; never starts one itself."""
    import httpx

    try:
        r = httpx.get(f"{base_url}/api/v1/ping", timeout=3.0)
        r.raise_for_status()
    except Exception:  # noqa: BLE001 - any failure means "not available"
        pytest.skip(f"no backend answering at {base_url}")
    return base_url
