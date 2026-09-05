"""Unit tests for custom extra output folders (catalog, watch, and index support)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.api.schemas.system import RootCreate
from app.api.v1 import system_router
from app.core import config_service
from app.core.pathsafe import path_key


def test_extra_output_dirs_configuration(temp_vault, tmp_path):
    """Test that extra_output_dirs in config resolves roots and output_dirs correctly."""
    custom_out = tmp_path / "my_custom_renders"
    custom_out.mkdir()

    config_service.set_config({"extra_output_dirs": [str(custom_out)]})
    cfg = config_service.get_config()

    assert any(r.kind == "extra_outputs" and r.path == str(custom_out.resolve()) for r in cfg.roots)

    dirs = config_service.output_dirs(cfg)
    assert any(d[0] == custom_out.resolve() and d[1].kind == "extra_outputs" for d in dirs)


def test_add_and_delete_extra_output_root(temp_vault, tmp_path):
    """Test adding and deleting an extra_outputs root via system router."""
    out_dir = tmp_path / "extra_shots"
    out_dir.mkdir()

    # Add extra output root
    added = system_router.add_root(RootCreate(path=str(out_dir), kind="extra_outputs"))
    assert added["kind"] == "extra_outputs"
    assert added["path"] == str(out_dir.resolve())
    root_id = added["id"]

    cfg = config_service.get_config()
    assert str(out_dir.resolve()) in [str(Path(d).resolve()) for d in cfg.extra_output_dirs]

    # Delete extra output root
    deleted = system_router.delete_root(root_id)
    assert deleted["deleted"] is True
    assert deleted["id"] == root_id

    cfg_after = config_service.get_config()
    assert str(out_dir.resolve()) not in [str(Path(d).resolve()) for d in cfg_after.extra_output_dirs]
