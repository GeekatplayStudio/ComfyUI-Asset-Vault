"""validate_comfyui_path / resolve_comfyui_dir - layout acceptance rules.

Three layouts must be told apart:

* a full install (source tree + models) - portable or git checkout,
* a ComfyUI Desktop *data folder* - assets only, ``main.py`` lives inside
  the Electron app.  Everything the indexer reads is present, so it is a
  valid target; only version/launch/update need the source tree,
* anything else - rejected with a reason.
"""

from __future__ import annotations

from app.core.config_service import (
    looks_like_comfyui,
    looks_like_comfyui_data,
    resolve_comfyui_dir,
    validate_comfyui_path,
)


def _full_install(base, name="ComfyUI"):
    root = base / name
    (root / "models" / "checkpoints").mkdir(parents=True)
    (root / "custom_nodes").mkdir()
    (root / "output").mkdir()
    (root / "main.py").write_text("# ComfyUI\n", encoding="utf-8")
    return root


def _data_folder(base, name="DATAFOLDER"):
    """ComfyUI Desktop's onboarding folder: assets, no Python source."""
    root = base / name
    (root / "models" / "checkpoints").mkdir(parents=True)
    (root / "custom_nodes").mkdir()
    (root / "output").mkdir()
    (root / "input").mkdir()
    (root / "user" / "default" / "workflows").mkdir(parents=True)
    return root


def test_a_full_install_is_valid_and_reported_as_full(tmp_path):
    root = _full_install(tmp_path)
    report = validate_comfyui_path(root)
    assert report["valid"] is True
    assert report["install_kind"] == "full"
    assert not report["issues"]


def test_a_desktop_data_folder_is_valid_and_reported_as_data(tmp_path):
    root = _data_folder(tmp_path)
    report = validate_comfyui_path(root)
    assert report["valid"] is True
    assert report["install_kind"] == "data_folder"
    assert not report["issues"]
    assert looks_like_comfyui_data(root)
    assert not looks_like_comfyui(root)


def test_models_alone_is_still_rejected(tmp_path):
    (tmp_path / "random" / "models").mkdir(parents=True)
    report = validate_comfyui_path(tmp_path / "random")
    assert report["valid"] is False
    assert report["install_kind"] is None
    assert report["issues"]


def test_a_missing_directory_is_rejected(tmp_path):
    report = validate_comfyui_path(tmp_path / "nope")
    assert report["valid"] is False
    assert "does not exist" in report["issues"][0]


def test_portable_parent_resolves_to_the_install_inside(tmp_path):
    root = _full_install(tmp_path)
    (tmp_path / "python_embeded").mkdir()
    assert resolve_comfyui_dir(tmp_path) == root
    report = validate_comfyui_path(tmp_path)
    assert report["valid"] is True
    assert report["install_kind"] == "full"
    assert report["resolved_from"] == str(tmp_path)


def test_parent_of_a_data_folder_resolves_into_it(tmp_path):
    root = _data_folder(tmp_path)
    assert resolve_comfyui_dir(tmp_path) == root
    report = validate_comfyui_path(tmp_path)
    assert report["valid"] is True
    assert report["install_kind"] == "data_folder"


def test_a_full_install_child_beats_a_data_folder_child(tmp_path):
    _data_folder(tmp_path, "a_data")
    full = _full_install(tmp_path, "b_full")
    assert resolve_comfyui_dir(tmp_path) == full


def test_a_typed_data_folder_is_not_overridden_by_children(tmp_path):
    root = _data_folder(tmp_path)
    _full_install(root, "nested")
    # The user pointed at the data folder itself; honour that.
    assert resolve_comfyui_dir(root) == root
