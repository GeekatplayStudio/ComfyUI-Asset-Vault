"""The self-updater may download a new app; it may not be talked into anything else.

Two properties are pinned here.  **Version comparison** has to be numeric, or
2.10.0 looks older than 2.9.0 and the update is never offered.  **Archive
handling** has to refuse a hostile zip outright, because this is the one
archive whose contents become the running application: traversal, absolute
paths, links, entry-count and expansion-ratio bombs, and anything outside the
directories a release is allowed to place.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.core.errors import ValidationError
from app.enable import hosts
from app.services import app_update_service as svc

# --------------------------------------------------------------- versions

@pytest.mark.parametrize("text,expected", [
    ("2.1.0", (2, 1, 0)),
    ("v2.1.0", (2, 1, 0)),
    ("V2.1.0", (2, 1, 0)),
    ("2.10.0", (2, 10, 0)),
    ("3", (3,)),
    ("", ()),
    (None, ()),
    ("not-a-version", ()),
])
def test_versions_parse_numerically(text, expected):
    assert svc.parse_version(text) == expected


@pytest.mark.parametrize("candidate,current,newer", [
    ("2.2.0", "2.1.0", True),
    ("2.1.1", "2.1.0", True),
    ("v2.2.0", "2.1.0", True),
    ("2.1.0", "2.1.0", False),
    ("2.0.9", "2.1.0", False),
    # The string-compare trap: "2.10.0" < "2.9.0" lexically, but 10 > 9.
    ("2.10.0", "2.9.0", True),
    ("2.9.0", "2.10.0", False),
    ("garbage", "2.1.0", False),
])
def test_only_a_genuinely_higher_version_counts_as_an_update(candidate, current, newer):
    assert svc.is_newer(candidate, current) is newer


# ------------------------------------------------------------ host policy

def test_the_release_hosts_are_allowlisted_and_nothing_else_is():
    for host in ("api.github.com", "github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com"):
        assert hosts.host_allowed(host, hosts.KIND_RELEASE), host
    for host in ("evil.com", "githubcom", "api.github.com.evil.com",
                 "civitai.com", "huggingface.co", "127.0.0.1"):
        assert not hosts.host_allowed(host, hosts.KIND_RELEASE), host


def test_a_release_url_must_be_https_and_on_the_list():
    checked = hosts.check("https://api.github.com/repos/x/y/releases/latest",
                          kind=hosts.KIND_RELEASE)
    assert checked.host == "api.github.com"
    for bad in ("http://api.github.com/x",          # not https
                "https://evil.com/release.zip",      # not allowlisted
                "https://user:pw@github.com/x.zip",  # credentials
                "https://1.2.3.4/x.zip"):            # bare IP
        with pytest.raises(hosts.HostNotAllowed):
            hosts.check(bad, kind=hosts.KIND_RELEASE)


def test_the_repository_is_pinned_not_configurable():
    """No setting may point the self-updater at somebody else's project."""
    assert svc.REPO_OWNER == "GeekatplayStudio"
    assert svc.REPO_NAME == "ComfyUI-Asset-Vault"
    assert svc.RELEASES_URL.startswith(
        "https://api.github.com/repos/GeekatplayStudio/ComfyUI-Asset-Vault/")


# ---------------------------------------------------------- archive safety

def _zip(entries: dict[str, bytes], *, prefix: str = "pkg-v9.9.9/") -> zipfile.ZipFile:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(prefix + name if prefix else name, data)
    buffer.seek(0)
    return zipfile.ZipFile(buffer)


def test_a_normal_release_maps_to_install_relative_paths():
    archive = _zip({
        "backend/app/main.py": b"x",
        "frontend/dist/index.html": b"y",
        "README.md": b"z",
    })
    mapped = dict(svc._safe_members(archive, 1024))
    assert sorted(mapped.values()) == [
        "README.md", "backend/app/main.py", "frontend/dist/index.html"]


def test_paths_outside_the_allowed_set_are_dropped():
    """A release cannot drop files into the owner's models or venv."""
    archive = _zip({
        "backend/app/main.py": b"x",
        "venv/Scripts/python.exe": b"nope",
        "backend/data/vault.db": b"nope",
        "models/checkpoints/evil.safetensors": b"nope",
        ".git/config": b"nope",
    })
    mapped = [rel for _name, rel in svc._safe_members(archive, 1024)]
    assert mapped == ["backend/app/main.py"]


@pytest.mark.parametrize("bad_name", [
    "../../evil.py",
    "backend/app/../../../evil.py",
    "C:/Windows/System32/evil.dll",
])
def test_traversal_and_absolute_paths_are_refused(bad_name):
    archive = _zip({"backend/app/main.py": b"x", bad_name: b"nope"})
    with pytest.raises(ValidationError):
        svc._safe_members(archive, 1024)


def test_a_link_entry_is_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("pkg/backend/app/main.py", b"x")
        info = zipfile.ZipInfo("pkg/backend/app/link.py")
        info.external_attr = (0o120777 << 16)  # S_IFLNK
        archive.writestr(info, "/etc/passwd")
    buffer.seek(0)
    with pytest.raises(ValidationError, match="link"):
        svc._safe_members(zipfile.ZipFile(buffer), 1024)


def test_an_absurd_entry_count_is_refused(monkeypatch):
    monkeypatch.setattr(svc, "MAX_ARCHIVE_ENTRIES", 5)
    archive = _zip({f"backend/app/m{i}.py": b"x" for i in range(10)})
    with pytest.raises(ValidationError, match="entries"):
        svc._safe_members(archive, 1024)


def test_a_zip_bomb_expansion_ratio_is_refused():
    """A 1 KB download that expands to 10 MB is not a release archive."""
    archive = _zip({"backend/app/big.py": b"0" * (10 * 1024 * 1024)})
    with pytest.raises(ValidationError, match="expands"):
        svc._safe_members(archive, 1024)


def test_an_archive_with_nothing_installable_is_refused():
    archive = _zip({"notes.txt": b"x", "pictures/cat.png": b"y"})
    with pytest.raises(ValidationError, match="nothing"):
        svc._safe_members(archive, 1024)


# --------------------------------------------------------------- staging

def test_pending_is_none_without_a_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "MARKER", tmp_path / "pending.json")
    monkeypatch.setattr(svc, "STAGE_DIR", tmp_path / "staged")
    assert svc.pending() is None


def test_a_marker_without_a_staged_tree_is_not_pending(tmp_path, monkeypatch):
    """A half-cleaned staging directory must not look like a ready update."""
    marker = tmp_path / "pending.json"
    marker.write_text('{"version": "9.9.9"}', encoding="utf-8")
    monkeypatch.setattr(svc, "MARKER", marker)
    monkeypatch.setattr(svc, "STAGE_DIR", tmp_path / "staged")
    assert svc.pending() is None


def test_discard_removes_the_marker_and_the_tree(tmp_path, monkeypatch):
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "x.py").write_text("x", encoding="utf-8")
    marker = tmp_path / "pending.json"
    marker.write_text('{"version": "9.9.9"}', encoding="utf-8")
    monkeypatch.setattr(svc, "MARKER", marker)
    monkeypatch.setattr(svc, "STAGE_DIR", staged)
    monkeypatch.setattr(svc, "DOWNLOAD_DIR", tmp_path / "download")

    assert svc.pending() is not None
    assert svc.discard() is True
    assert not marker.exists()
    assert not staged.exists()
    assert svc.pending() is None


# ---------------------------------------------------------------- status

def test_status_reports_offline_rather_than_reaching_out(temp_vault, monkeypatch):
    from app.core import config_service

    config_service.set_config({"online_enabled": False,
                               "app_update_check_enabled": True})

    def boom(*a, **kw):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("the kill-switch must be checked before any request")

    monkeypatch.setattr(svc, "_get", boom)
    result = svc.status()
    assert result["state"] == "offline"
    assert result["has_update"] is False
    assert result["current_version"]


def test_status_reports_disabled_when_checks_are_off(temp_vault, monkeypatch):
    from app.core import config_service

    config_service.set_config({"online_enabled": True,
                               "app_update_check_enabled": False})

    def boom(*a, **kw):  # pragma: no cover
        raise AssertionError("a disabled check must not reach the network")

    monkeypatch.setattr(svc, "_get", boom)
    assert svc.status()["state"] == "disabled"
