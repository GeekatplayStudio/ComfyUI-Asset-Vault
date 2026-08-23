"""Path containment and filename validation - the boundary every write crosses.

This file replaces the v1 test ``test_file_ops.py::test_is_safe_path_protection``,
which asserted only that an obvious ``..\\..\\Windows`` string was refused.  That
is the easy half.  The containment bugs that actually ship are quieter:

* **the prefix bug** - comparing normalized strings with ``startswith`` puts
  ``C:\\a\\comfyui-other`` "inside" the root ``C:\\a\\comfy``, because the root's
  text really is a prefix of the sibling's.  Every delete, move and rename in the
  app is gated on this one predicate, so the sibling directory is the case that
  matters most and it gets an explicit test below;
* **traversal that normalizes late** - a ``..`` segment only escapes once the path
  is resolved, so the check has to run on the resolved form, not the raw string;
* **names that are legal to Python and fatal to Windows** - ``CON``, ``LPT1``, a
  trailing dot or space.  Windows resolves those to devices or silently strips
  the trailing character, which turns a create into an overwrite of something else.

Non-ASCII names are the mirror image: a model library is full of CJK and emoji
filenames, and rejecting those would break real files in the name of safety.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.errors import PathNotAllowed, ValidationError
from app.core.pathsafe import (
    LONG_PREFIX,
    Root,
    is_contained,
    long_path,
    normalize,
    path_key,
    resolve_within_roots,
    safe_relpath,
    validate_filename,
)

ROOT = r"C:\ComfyUI\models"
SIBLING = r"C:\ComfyUI\models-backup"


def _root(path: str, root_id: int = 1, kind: str = "models") -> Root:
    return Root(id=root_id, kind=kind, path=path, label=f"root-{root_id}")


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------

def test_normalize_resolves_traversal_segments() -> None:
    """Containment is decided on the normalized form, so ``..`` has to collapse
    here or every later check is comparing the wrong path."""
    assert normalize(r"C:\ComfyUI\models\..\output\x.png") == Path(r"C:\ComfyUI\output\x.png")


def test_normalize_strips_the_long_path_prefix() -> None:
    """The same file reaches the app both with and without the ``\\\\?\\`` prefix;
    if only one form normalized, one file would occupy two rows."""
    assert normalize(LONG_PREFIX + r"C:\ComfyUI\models") == normalize(r"C:\ComfyUI\models")


def test_normalize_restores_a_unc_share_from_its_prefixed_form(monkeypatch) -> None:
    """Network model folders are common, and ``\\\\?\\UNC\\server\\share`` has to
    fold back to ``\\\\server\\share`` rather than losing its leading separators
    and becoming a relative path.

    ``realpath`` is stubbed out here on purpose: resolving an unreachable share
    costs several seconds of name resolution, and the behaviour under test is the
    prefix rewriting that happens before that call.
    """
    monkeypatch.setattr(os.path, "realpath", lambda s, **_kw: s)

    got = normalize(LONG_PREFIX + r"UNC\server\share\models")

    assert str(got) == r"\\server\share\models"


def test_normalize_returns_a_path_and_never_raises() -> None:
    """It sits on the request path for user-supplied strings, so a malformed input
    has to produce a value the caller can then reject deliberately."""
    for value in ["", ".", "relative/fragment", "C:", "\\\\", "C:\\a\\\\\\b"]:
        assert isinstance(normalize(value), Path)


def test_normalize_makes_relative_input_absolute() -> None:
    """A relative path is meaningless once it is stored; leaving one unresolved
    would make the row depend on the process working directory."""
    assert normalize("models/loras").is_absolute()


def test_normalize_is_idempotent() -> None:
    """Paths get normalized at several layers; a second pass must be a no-op or
    the value stored differs from the value compared."""
    once = normalize(r"C:\ComfyUI\models\..\models\loras")

    assert normalize(once) == once


# ---------------------------------------------------------------------------
# path_key
# ---------------------------------------------------------------------------

def test_path_key_folds_case_and_separator_spelling() -> None:
    """The key backs a UNIQUE constraint.  NTFS is case-insensitive, so two
    spellings of one file must collide in the index rather than create a duplicate
    row that later fights with itself over the same bytes."""
    assert path_key(r"C:\ComfyUI\Models\SD15.safetensors") == \
        path_key("c:/comfyui/models/sd15.safetensors")


def test_path_key_separates_different_files() -> None:
    """Folding must stop at spelling; two real files may not share a key."""
    assert path_key(r"C:\ComfyUI\models\a.safetensors") != \
        path_key(r"C:\ComfyUI\models\b.safetensors")


def test_path_key_is_a_plain_string() -> None:
    """It is bound straight into SQL, so it may not be a ``Path`` that stringifies
    differently on another platform."""
    key = path_key(ROOT)

    assert isinstance(key, str)
    assert key == path_key(Path(ROOT))


# ---------------------------------------------------------------------------
# long_path
# ---------------------------------------------------------------------------

def test_short_paths_are_left_alone() -> None:
    """The prefix disables path parsing in the Win32 layer; applying it to every
    path would change how relative and drive-relative forms resolve."""
    assert long_path(r"C:\ComfyUI\models\sd15.safetensors") == \
        r"C:\ComfyUI\models\sd15.safetensors"


@pytest.mark.skipif(os.name != "nt", reason="the long-path prefix is a Win32 concept")
def test_long_paths_get_the_win32_prefix() -> None:
    """Past MAX_PATH the ordinary open() fails outright.  Deeply nested LoRA
    folders reach that length routinely, and those files must stay reachable."""
    deep = "C:\\" + "\\".join(f"folder_{i:03d}" for i in range(30)) + "\\model.safetensors"

    assert len(deep) > 260
    assert long_path(deep).startswith(LONG_PREFIX)
    assert long_path(deep).endswith("model.safetensors")


@pytest.mark.skipif(os.name != "nt", reason="the long-path prefix is a Win32 concept")
def test_long_unc_paths_get_the_unc_form_of_the_prefix() -> None:
    """A network share needs ``\\\\?\\UNC\\server\\share``; the plain prefix in
    front of ``\\\\server`` is not a path Win32 accepts."""
    deep = "\\\\nas\\models\\" + "\\".join(f"folder_{i:03d}" for i in range(30))

    assert len(deep) > 260
    assert long_path(deep).startswith(LONG_PREFIX + "UNC\\")


def test_an_already_prefixed_path_is_not_prefixed_twice() -> None:
    """Prefixing is applied at several call sites; doubling it produces a path no
    API will open."""
    already = LONG_PREFIX + "C:\\" + "\\".join(f"folder_{i:03d}" for i in range(30))

    assert long_path(already) == already


def test_long_path_returns_a_string() -> None:
    """It feeds ``open()`` and the ``os`` module directly, where a ``Path`` would
    be re-parsed and lose the prefix."""
    assert isinstance(long_path(Path(ROOT)), str)


# ---------------------------------------------------------------------------
# is_contained - the predicate every destructive operation is gated on
# ---------------------------------------------------------------------------

def test_a_file_inside_the_root_is_contained() -> None:
    """The base case: without it nothing in the app could be modified at all."""
    assert is_contained(r"C:\ComfyUI\models\loras\detail.safetensors", ROOT) is True


def test_a_deeply_nested_file_is_contained() -> None:
    """Users organise models into subfolders many levels deep."""
    assert is_contained(r"C:\ComfyUI\models\loras\style\anime\v2\x.safetensors", ROOT) is True


def test_the_root_itself_is_contained() -> None:
    """Containment is inclusive: operations that target the root - listing it,
    fingerprinting it - would otherwise be refused against their own root."""
    assert is_contained(ROOT, ROOT) is True


def test_a_sibling_whose_name_starts_with_the_root_name_is_not_contained() -> None:
    """The classic prefix bug.  ``C:\\ComfyUI\\models`` is a literal string prefix
    of ``C:\\ComfyUI\\models-backup``, so any implementation built on
    ``startswith`` hands the app write access to a directory the user never added
    as a root - and "models-backup" is exactly the folder a user creates before
    reorganising their library."""
    assert is_contained(SIBLING, ROOT) is False
    assert is_contained(SIBLING + r"\sd15.safetensors", ROOT) is False


@pytest.mark.parametrize("sibling", [
    r"C:\ComfyUI\models_old",
    r"C:\ComfyUI\models2",
    r"C:\ComfyUI\modelsX\deep\nested\file.safetensors",
    r"C:\ComfyUI\models.bak\sd15.safetensors",
])
def test_no_sibling_sharing_the_roots_prefix_is_contained(sibling: str) -> None:
    """The bug has more than one spelling: a separator, a digit, a dot or a letter
    can each follow the root's name and still leave it a string prefix."""
    assert is_contained(sibling, ROOT) is False


def test_traversal_out_of_the_root_is_rejected() -> None:
    """The v1 case, kept: an API argument carrying ``..`` must not reach outside
    the root even though every character of the root is still in the string."""
    assert is_contained(ROOT + r"\..\..\Windows\System32\drivers\etc\hosts", ROOT) is False
    assert is_contained(ROOT + r"\loras\..\..\output\x.png", ROOT) is False


def test_traversal_that_stays_inside_the_root_is_allowed() -> None:
    """Refusing every ``..`` would be safe but wrong; paths assembled from a folder
    plus a relative name legitimately contain one."""
    assert is_contained(ROOT + r"\loras\..\checkpoints\sd15.safetensors", ROOT) is True


@pytest.mark.skipif(os.name != "nt", reason="drive letters are a Win32 concept")
def test_a_path_on_another_drive_is_not_contained() -> None:
    """Model libraries commonly span drives, and comparing path components without
    the drive would make ``D:\\ComfyUI\\models`` look like the ``C:`` root."""
    assert is_contained(r"D:\ComfyUI\models\sd15.safetensors", ROOT) is False


def test_an_empty_root_contains_nothing() -> None:
    """An unconfigured root arrives as an empty string; treating that as "matches
    everything" would open the whole filesystem on a fresh install."""
    assert is_contained(r"C:\ComfyUI\models\sd15.safetensors", "") is False
    assert is_contained(r"C:\ComfyUI\models\sd15.safetensors", ".") is False


def test_containment_ignores_case_and_separator_spelling() -> None:
    """A root read from the config and a path read from a directory walk rarely
    agree on casing; refusing the mismatch would break every operation."""
    assert is_contained("c:/comfyui/models/loras/x.safetensors", r"C:\ComfyUI\Models") is True


def test_containment_holds_for_directories_that_really_exist(tmp_path: Path) -> None:
    """Normalization resolves symlinks and short names against the filesystem, so
    the predicate is also exercised against real directories rather than only
    against strings that never touch a disk."""
    root = tmp_path / "comfy"
    (root / "models" / "loras").mkdir(parents=True)
    sibling = tmp_path / "comfyui-other"
    sibling.mkdir()
    inside = root / "models" / "loras" / "detail.safetensors"
    inside.write_bytes(b"probe")

    assert is_contained(inside, root) is True
    assert is_contained(sibling, root) is False
    assert is_contained(sibling / "detail.safetensors", root) is False
    assert is_contained(root / ".." / "comfyui-other", root) is False


# ---------------------------------------------------------------------------
# resolve_within_roots
# ---------------------------------------------------------------------------

def test_resolve_returns_the_normalized_path_and_its_root() -> None:
    """Callers store the returned path, so it has to be the resolved form rather
    than whatever spelling the request happened to use."""
    roots = [_root(ROOT), _root(r"C:\ComfyUI\output", 2, "output")]

    target, owner = resolve_within_roots("c:/comfyui/models/loras/../sd15.safetensors", roots)

    assert target == Path(r"C:\ComfyUI\models\sd15.safetensors")
    assert owner.id == 1


def test_resolve_prefers_the_deepest_matching_root() -> None:
    """Users add a nested root - a LoRA folder on a second drive mounted inside
    the model tree - and the file belongs to that root, not to its ancestor."""
    roots = [_root(ROOT), _root(r"C:\ComfyUI\models\loras", 2, "loras")]

    _target, owner = resolve_within_roots(r"C:\ComfyUI\models\loras\detail.safetensors", roots)

    assert owner.id == 2


def test_resolve_rejects_an_absolute_path_outside_every_root() -> None:
    """The gate itself: an absolute path in a request must not be honoured just
    because it is well-formed."""
    roots = [_root(ROOT)]

    with pytest.raises(PathNotAllowed) as exc:
        resolve_within_roots(r"C:\Windows\System32\drivers\etc\hosts", roots)

    assert exc.value.code == "PATH_NOT_ALLOWED"
    assert exc.value.http_status == 403


def test_resolve_rejects_a_traversal_that_leaves_the_root() -> None:
    """Same rejection, reached the other way - the string starts inside the root
    and only escapes once it is resolved."""
    roots = [_root(ROOT)]

    with pytest.raises(PathNotAllowed):
        resolve_within_roots(ROOT + r"\..\..\Windows\win.ini", roots)


def test_resolve_rejects_a_sibling_of_the_root() -> None:
    """The prefix bug again, at the layer that actually authorizes writes."""
    roots = [_root(ROOT)]

    with pytest.raises(PathNotAllowed):
        resolve_within_roots(SIBLING + r"\sd15.safetensors", roots)


def test_resolve_with_no_roots_configured_rejects_everything() -> None:
    """Before setup there is no root, and "no roots" must mean "nothing allowed"
    rather than an empty loop that falls through to success."""
    with pytest.raises(PathNotAllowed):
        resolve_within_roots(r"C:\ComfyUI\models\sd15.safetensors", [])


def test_resolve_reports_the_offending_path_in_its_details() -> None:
    """The API renders these details; without the path the user cannot tell which
    of several paths in a batch request was refused."""
    with pytest.raises(PathNotAllowed) as exc:
        resolve_within_roots(r"C:\Windows\win.ini", [_root(ROOT)])

    assert "win.ini" in str(exc.value.details.get("path", "")).lower()


# ---------------------------------------------------------------------------
# validate_filename
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "sub\\evil.safetensors",
    "sub/evil.safetensors",
    "..\\escape.safetensors",
    "../escape.safetensors",
    r"C:\Windows\System32\evil.dll",
    "\\\\server\\share\\evil.safetensors",
])
def test_names_carrying_a_path_are_rejected(name: str) -> None:
    """A rename is a single component by contract.  A separator inside it turns
    "rename this file" into "move it anywhere", and the containment check upstream
    already ran against the *old* path."""
    with pytest.raises(ValidationError):
        validate_filename(name)


@pytest.mark.parametrize("name", ["CON", "PRN", "AUX", "NUL", "COM1", "LPT1",
                                  "con.txt", "nul.safetensors", "Com1.json",
                                  "LPT9.png"])
def test_reserved_windows_device_names_are_rejected(name: str) -> None:
    """Windows resolves these to devices at any directory depth and with any
    extension.  Writing to ``NUL.safetensors`` discards the bytes and reports
    success, so the file appears to save and is simply gone."""
    with pytest.raises(ValidationError):
        validate_filename(name)


def test_a_name_merely_starting_with_a_device_name_is_accepted() -> None:
    """The device check is on the stem, not a prefix match; ``CONTROLNET`` and
    ``AUXILIARY`` are ordinary names and refusing them would be a false positive
    on real files."""
    validate_filename("CONTROLNET.safetensors")
    validate_filename("auxiliary-notes.txt")
    validate_filename("computer.png")


@pytest.mark.parametrize("name", ["model.", "model ", "model..", "trailing space "])
def test_trailing_dots_and_spaces_are_rejected(name: str) -> None:
    """Win32 strips them silently, so creating ``model.`` yields ``model`` - which
    may already exist.  The create then overwrites a different file, and the
    conflict check that ran against the requested name never fired."""
    with pytest.raises(ValidationError):
        validate_filename(name)


@pytest.mark.parametrize("name", ["", "   ", "\t", "\n"])
def test_empty_and_blank_names_are_rejected(name: str) -> None:
    """An empty name resolves to the parent directory, turning a file operation
    into a directory operation."""
    with pytest.raises(ValidationError):
        validate_filename(name)


@pytest.mark.parametrize("name", [".", ".."])
def test_the_directory_entries_are_rejected(name: str) -> None:
    """``.`` and ``..`` are legal single components and mean the wrong thing."""
    with pytest.raises(ValidationError):
        validate_filename(name)


@pytest.mark.parametrize("name", ['quote".safetensors', "pipe|.safetensors",
                                  "star*.safetensors", "question?.safetensors",
                                  "lt<.safetensors", "gt>.safetensors",
                                  "colon:.safetensors"])
def test_characters_windows_forbids_are_rejected(name: str) -> None:
    """A colon opens an NTFS alternate data stream - bytes written to
    ``model.safetensors:hidden`` do not appear in any listing - and the wildcards
    would be expanded by any shell the path is later handed to."""
    with pytest.raises(ValidationError):
        validate_filename(name)


@pytest.mark.parametrize("name", ["bell\x07.safetensors", "null\x00.safetensors",
                                  "newline\n.safetensors", "esc\x1b[31m.safetensors"])
def test_control_characters_are_rejected(name: str) -> None:
    """A NUL truncates the name inside the Win32 layer, and an escape sequence in
    a name is rendered by any terminal that later prints a scan log."""
    with pytest.raises(ValidationError):
        validate_filename(name)


@pytest.mark.parametrize("name", [
    "模型_v2.safetensors",
    "モデル.safetensors",
    "модель.safetensors",
    "café-lora.safetensors",
    "🎨-style-v1.safetensors",
    "Ω-omega.safetensors",
    "한국어-모델.safetensors",
    "naïve_résumé.png",
])
def test_non_ascii_names_are_accepted(name: str) -> None:
    """NTFS stores UTF-16 names and model libraries are full of them.  Rejecting
    non-ASCII would be the more damaging bug: it would make real files on the
    user's disk unrenameable and unmovable through the app."""
    validate_filename(name)


@pytest.mark.parametrize("name", ["sd15.safetensors", "my model v2.ckpt",
                                  "a", "lora_v1.2.3.safetensors", ".gitignore",
                                  "a" * 255])
def test_ordinary_names_are_accepted(name: str) -> None:
    """The rejections above have to stay narrow; a validator that fails closed on
    normal names is just as broken, only quieter."""
    validate_filename(name)


# Fixed: pathsafe.MAX_COMPONENT_CHARS.  A failure here means the length limit was
# removed and an over-long component can again reach mkdir/rename as a raw OSError.
def test_names_over_the_filesystem_limit_are_rejected() -> None:
    """A single path component may not exceed 255 characters on NTFS.  Catching
    that here keeps it a clean 422 with a message the user can act on, instead of
    an OSError surfacing from whichever syscall runs first - which is also the
    only thing standing between a long name and a partially created folder tree."""
    with pytest.raises(ValidationError):
        validate_filename("a" * 300 + ".safetensors")


# ---------------------------------------------------------------------------
# safe_relpath
# ---------------------------------------------------------------------------

def test_safe_relpath_of_a_file_under_its_root() -> None:
    """The relative path is what the UI shows and what the index groups on."""
    assert safe_relpath(r"C:\ComfyUI\models\loras\detail.safetensors", ROOT) == \
        r"loras\detail.safetensors"


def test_safe_relpath_of_the_root_itself() -> None:
    """A root is its own container, and the walk starts by asking for it."""
    assert safe_relpath(ROOT, ROOT) == "."


def test_safe_relpath_falls_back_to_the_basename_outside_the_root() -> None:
    """``os.path.relpath`` would answer with a ``..`` chain that walks out of the
    root; storing that would put an escaping path in the database and hand it to
    the next caller as if it were relative."""
    rel = safe_relpath(SIBLING + r"\sd15.safetensors", ROOT)

    assert rel == "sd15.safetensors"
    assert ".." not in rel


def test_safe_relpath_across_drives_falls_back_to_the_basename() -> None:
    """There is no relative path between two drives; ``relpath`` raises, and the
    raise must not escape into a directory walk."""
    assert safe_relpath(r"D:\other\sd15.safetensors", ROOT) == "sd15.safetensors"
