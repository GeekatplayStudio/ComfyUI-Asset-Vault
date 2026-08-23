r"""Unit-level containment tests for ``app/core/pathsafe.py``.

Windows path handling is where a "root guard" usually dies, so every shape the
audit checklist names is exercised here against the real function, on the real
filesystem where the answer depends on it: ``..``, percent-encoded ``..``, UNC,
``\\?\``, alternate data streams, 8.3 short names, junctions, drive switching,
case-only differences, trailing dots and spaces, and paths over 260 characters.

See docs/SECURITY_REVIEW.md finding S-01 for the one shape that is *not* stopped
here (NTFS junctions are stopped at ``is_contained``, but the indexing walker
descends them before ``is_contained`` is ever consulted - covered in
test_traversal.py).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from app.core.errors import PathNotAllowed, ValidationError
from app.core.pathsafe import (
    Root,
    is_contained,
    long_path,
    normalize,
    path_key,
    resolve_within_roots,
    safe_relpath,
    validate_filename,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="Windows-specific path semantics")


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "vault" / "ComfyUI"
    (r / "models" / "checkpoints").mkdir(parents=True)
    (tmp_path / "vault" / "SIBLING").mkdir(parents=True, exist_ok=True)
    (tmp_path / "OUTSIDE").mkdir(exist_ok=True)
    return r


# ---------------------------------------------------------------------------
# is_contained - the single decision every file operation rests on
# ---------------------------------------------------------------------------

def test_plain_child_is_contained(root):
    assert is_contained(root / "models" / "checkpoints" / "a.safetensors", root)


def test_root_itself_is_contained(root):
    assert is_contained(root, root)


def test_dotdot_escape_is_rejected(root):
    assert not is_contained(str(root) + r"\..\..\OUTSIDE\evil.txt", root)
    assert not is_contained(str(root) + r"\models\..\..\..\OUTSIDE\evil.txt", root)


def test_forward_slash_dotdot_escape_is_rejected(root):
    assert not is_contained(str(root).replace("\\", "/") + "/models/../../evil.txt", root)


def test_percent_encoded_dotdot_is_not_decoded(root):
    """``%2e%2e`` must stay a literal directory name, never become ``..``."""
    target = str(root) + r"\%2e%2e\evil.txt"
    assert is_contained(target, root)
    assert "%2e%2e" in str(normalize(target))


def test_sibling_directory_sharing_a_prefix_is_not_contained(tmp_path):
    root = tmp_path / "ComfyUI"
    root.mkdir()
    (tmp_path / "ComfyUI-other").mkdir()
    assert not is_contained(tmp_path / "ComfyUI-other" / "x.txt", root)


def test_case_only_difference_is_still_contained(root):
    assert is_contained(str(root).upper() + r"\MODELS\A.SAFETENSORS", root)


def test_drive_switch_is_rejected(root):
    other = ("D:" if str(root)[0].upper() != "D" else "C:") + r"\ComfyUI\models\a.txt"
    assert not is_contained(other, root)


def test_unc_path_is_not_contained_in_a_local_root(root):
    assert not is_contained(r"\\fileserver\share\models\a.safetensors", root)
    assert not is_contained(r"\\?\UNC\fileserver\share\a.safetensors", root)


def test_unc_root_containment_is_computed_correctly():
    assert is_contained(r"\\fileserver\share\models\a.safetensors",
                        r"\\fileserver\share")
    assert not is_contained(r"\\otherserver\share\a.safetensors",
                            r"\\fileserver\share")


def test_long_path_prefix_is_stripped_before_comparison(root):
    assert is_contained("\\\\?\\" + str(root) + r"\models\a.safetensors", root)


def test_eight_dot_three_short_name_is_expanded(tmp_path):
    """``PROGRA~1`` must resolve to its long form before the comparison."""
    if not os.path.isdir(r"C:\Program Files"):
        pytest.skip("no C:\\Program Files on this machine")
    assert is_contained(r"C:\PROGRA~1\somefile.txt", r"C:\Program Files")


def test_path_over_260_chars_is_handled_without_raising(root):
    deep = str(root) + "\\" + "\\".join(f"segment{i:03d}" for i in range(30)) + "\\f.bin"
    assert len(deep) > 260
    assert is_contained(deep, root)
    assert long_path(deep).startswith("\\\\?\\")


def test_trailing_dot_and_space_components_never_escape(root):
    """Win32 strips trailing dots/spaces, so ``.. `` must not become ``..``."""
    for tail in (r"\.. \OUTSIDE", r"\... \OUTSIDE", r"\.. .\OUTSIDE", r"\...\OUTSIDE"):
        assert not is_contained(str(root.parent) + tail, root), tail


def test_alternate_data_stream_on_a_contained_file_stays_contained(root):
    """An ADS lives on a file inside the root, so containment is correct here;
    the ADS is blocked at the *name* layer instead (see validate_filename)."""
    assert is_contained(str(root) + r"\models\a.safetensors:hidden", root)


def test_empty_root_is_never_contained(root):
    assert not is_contained(root / "x", "")


def test_is_contained_never_raises(root):
    for junk in ("", ".", "?", "\x00", "C:", "::", "\\\\", 5, None):
        try:
            is_contained(junk, root)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"is_contained raised on {junk!r}: {exc}")


# ---------------------------------------------------------------------------
# Junctions and symlinks
# ---------------------------------------------------------------------------

def _mklink_junction(link, target) -> bool:
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    result = subprocess.run(  # noqa: S603
        [comspec, "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, check=False)
    return result.returncode == 0


def test_junction_target_is_resolved_and_rejected(root, tmp_path):
    """``is_contained`` itself is correct: realpath follows the junction."""
    outside = tmp_path / "OUTSIDE"
    (outside / "secret.safetensors").write_bytes(b"\0" * 16)
    link = root / "models" / "checkpoints" / "linked"
    if not _mklink_junction(link, outside):
        pytest.skip("could not create an NTFS junction here")
    through = link / "secret.safetensors"
    assert through.is_file()
    assert not is_contained(through, root), (
        "a junction target outside the root must not be reported as contained")


# ---------------------------------------------------------------------------
# resolve_within_roots
# ---------------------------------------------------------------------------

def test_resolve_picks_the_deepest_matching_root(root):
    roots = [Root(id=1, kind="comfyui", path=str(root), label="root"),
             Root(id=2, kind="models", path=str(root / "models" / "checkpoints"),
                  label="ckpt")]
    _path, chosen = resolve_within_roots(root / "models" / "checkpoints" / "a.st", roots)
    assert chosen.id == 2


def test_resolve_outside_every_root_raises(root, tmp_path):
    roots = [Root(id=1, kind="comfyui", path=str(root), label="root")]
    with pytest.raises(PathNotAllowed):
        resolve_within_roots(tmp_path / "OUTSIDE" / "evil.txt", roots)


def test_resolve_with_no_roots_raises(root):
    with pytest.raises(PathNotAllowed):
        resolve_within_roots(root / "models" / "a.st", [])


# ---------------------------------------------------------------------------
# validate_filename - the rename guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "..", ".", "../evil", "..\\evil", "a/b", "a\\b",
    "C:\\evil.safetensors", "\\\\server\\share\\x",
    "evil.safetensors:ads", "evil.safetensors::$DATA",
    "trailing.", "trailing ", "....", "  ", "",
    "CON", "con.txt", "NUL", "AUX", "COM1.txt", "LPT9",
    "bad<name>.txt", "pipe|name.txt", "quote\"name.txt", "star*.txt",
    "null\x00byte.txt", "bell\x07.txt",
])
def test_validate_filename_rejects(name):
    with pytest.raises(ValidationError):
        validate_filename(name)


@pytest.mark.parametrize("name", [
    "model.safetensors", "a..b.safetensors", "..leading.safetensors",
    "\u6a21\u578b.safetensors", "caf\u00e9.png", "\U0001f3a8-art.png",
    "CONTOUR.safetensors", "space in name.safetensors",
])
def test_validate_filename_accepts(name):
    validate_filename(name)


# ---------------------------------------------------------------------------
# path_key / safe_relpath
# ---------------------------------------------------------------------------

def test_path_key_is_case_and_separator_insensitive():
    assert path_key(r"C:\A\B\c.txt") == path_key("c:/a/b/C.TXT")


def test_safe_relpath_never_returns_a_traversal(root, tmp_path):
    rel = safe_relpath(tmp_path / "OUTSIDE" / "evil.txt", root)
    assert not rel.startswith("..")
    assert rel == "evil.txt"
