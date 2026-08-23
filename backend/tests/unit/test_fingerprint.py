"""Incremental-scan fingerprints: cheap, stable, and sensitive to real changes.

A rescan of a large library only reparses files whose fingerprint moved, so these
four functions decide what work happens.  Two failure modes matter and they pull
in opposite directions.  If a fingerprint is *unstable* - it changes across runs
for an unchanged file - every scan reparses the whole library.  If it is *blind* -
it stays the same after a file really changed - the vault serves stale metadata
forever, and nothing short of a manual reindex fixes it.

The path handling is deliberately Windows-shaped: ``normcase`` folds case and
separators, so the same file reached by two spellings must fingerprint once.
These tests assert the behaviour the implementation actually has on each
platform rather than a portable ideal it does not implement.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import pytest

from app.core.fingerprint import file_fingerprint, folder_fingerprint, path_hash, text_hash

PATH = r"C:\ComfyUI\models\checkpoints\sd15.safetensors"
SIZE = 2_132_625_432
MTIME = 1_723_305_600_123_456_700

ENTRIES = [
    ("checkpoints\\sd15.safetensors", 2_132_625_432, 1_723_305_600_000_000_000),
    ("loras\\detail.safetensors", 151_117_312, 1_723_305_700_000_000_000),
    ("vae\\sdxl-vae.safetensors", 334_641_162, 1_723_305_800_000_000_000),
]


# ---------------------------------------------------------------------------
# file_fingerprint
# ---------------------------------------------------------------------------

def test_file_fingerprint_is_stable_across_calls() -> None:
    """Instability here would make every rescan a full rescan of a library that
    can run to terabytes."""
    assert file_fingerprint(PATH, SIZE, MTIME) == file_fingerprint(PATH, SIZE, MTIME)


def test_file_fingerprint_is_a_plain_storable_string() -> None:
    """The value goes into a SQLite text column and into JSON responses, so it has
    to be printable ASCII with no encoding of its own."""
    fp = file_fingerprint(PATH, SIZE, MTIME)

    assert isinstance(fp, str)
    assert fp.isascii()
    assert set(fp) <= set("0123456789abcdef")
    assert len(fp) == 32
    assert fp == fp.strip()


def test_file_fingerprint_accepts_a_path_object() -> None:
    """Callers hold ``Path`` objects; a str/Path split would silently reindex the
    whole library the first time a call site changed type."""
    assert file_fingerprint(Path(PATH), SIZE, MTIME) == file_fingerprint(PATH, SIZE, MTIME)


def test_file_fingerprint_tracks_size() -> None:
    """A re-download that lands at a different size is the loudest possible signal
    that the bytes changed."""
    assert file_fingerprint(PATH, SIZE, MTIME) != file_fingerprint(PATH, SIZE + 1, MTIME)


def test_file_fingerprint_tracks_mtime() -> None:
    """A file replaced in place keeps its size; without mtime the swap would be
    invisible and the vault would keep serving the old metadata."""
    assert file_fingerprint(PATH, SIZE, MTIME) != file_fingerprint(PATH, SIZE, MTIME + 1)


def test_file_fingerprint_tracks_path() -> None:
    """Two identically sized files copied at the same moment are common in a model
    library; the path is what keeps their rows apart."""
    other = r"C:\ComfyUI\models\checkpoints\sd21.safetensors"

    assert file_fingerprint(PATH, SIZE, MTIME) != file_fingerprint(other, SIZE, MTIME)


def test_field_separators_prevent_boundary_collisions() -> None:
    """Concatenating the fields without a delimiter would make (1, 23) and (12, 3)
    hash identically, so a resized file could keep its old fingerprint."""
    assert file_fingerprint(PATH, 1, 23) != file_fingerprint(PATH, 12, 3)


def test_file_fingerprint_normalizes_the_path_spelling() -> None:
    """The same file arrives spelled differently depending on which layer produced
    the path - the config, a directory walk, or a URL-decoded API argument.  On
    Windows those spellings name one file and must fingerprint once; elsewhere
    they are genuinely different files."""
    variants = [
        file_fingerprint(PATH, SIZE, MTIME),
        file_fingerprint(PATH.upper(), SIZE, MTIME),
        file_fingerprint(PATH.replace("\\", "/"), SIZE, MTIME),
    ]

    if os.name == "nt":
        assert variants[0] == variants[1] == variants[2]
    else:
        assert len(set(variants)) == 3


def test_file_fingerprint_is_collision_free_over_a_realistic_library() -> None:
    """Six hundred distinct files standing in for a real ``models`` tree; a single
    collision would leave one of them permanently unscanned."""
    seen = {
        file_fingerprint(rf"C:\ComfyUI\models\checkpoints\model_{i:04d}.safetensors",
                         1_000_000 + i, MTIME + i * 1_000_000)
        for i in range(600)
    }

    assert len(seen) == 600


# ---------------------------------------------------------------------------
# folder_fingerprint
# ---------------------------------------------------------------------------

def test_folder_fingerprint_is_order_independent() -> None:
    """The implementation sorts its entries, which is what makes it usable: the
    OS returns directory entries in whatever order the filesystem likes, and that
    order is not stable across runs or across machines."""
    baseline = folder_fingerprint(ENTRIES)
    shuffled = list(ENTRIES)
    random.Random(7).shuffle(shuffled)

    assert folder_fingerprint(shuffled) == baseline
    assert folder_fingerprint(list(reversed(ENTRIES))) == baseline


def test_folder_fingerprint_accepts_any_iterable() -> None:
    """Directory walks hand back generators, and consuming one twice yields
    nothing the second time."""
    assert folder_fingerprint(iter(list(ENTRIES))) == folder_fingerprint(ENTRIES)
    assert folder_fingerprint(tuple(ENTRIES)) == folder_fingerprint(ENTRIES)


@pytest.mark.parametrize(
    ("label", "entries"),
    [
        ("renamed", [("checkpoints\\sd15-v2.safetensors", 2_132_625_432,
                      1_723_305_600_000_000_000), *ENTRIES[1:]]),
        ("resized", [(ENTRIES[0][0], ENTRIES[0][1] + 1, ENTRIES[0][2]), *ENTRIES[1:]]),
        ("retouched", [(ENTRIES[0][0], ENTRIES[0][1], ENTRIES[0][2] + 1), *ENTRIES[1:]]),
        ("removed", ENTRIES[:2]),
        ("added", [*ENTRIES, ("vae\\extra.safetensors", 12_345, 1_723_305_900_000_000_000)]),
        ("emptied", []),
    ],
)
def test_folder_fingerprint_changes_on_any_entry_change(label: str, entries: list) -> None:
    """Every one of these is a change the user made and expects to see reflected;
    a fingerprint that missed one would skip the folder on the next scan."""
    assert folder_fingerprint(entries) != folder_fingerprint(ENTRIES), label


def test_folder_fingerprint_is_stable_for_an_unchanged_folder() -> None:
    """The whole point of the folder-level check is to skip untouched folders
    without stating them one by one."""
    assert folder_fingerprint(list(ENTRIES)) == folder_fingerprint(list(ENTRIES))


def test_folder_fingerprint_of_an_empty_folder_is_defined() -> None:
    """Empty model folders are ordinary - a fresh install has several - and must
    produce a value rather than an error or an empty string."""
    fp = folder_fingerprint([])

    assert isinstance(fp, str)
    assert len(fp) == 32
    assert fp == folder_fingerprint(())


def test_folder_fingerprint_normalizes_entry_spelling() -> None:
    """Relative entries inherit the walk's separator and casing, so the same
    folder walked from two starting points must not look changed."""
    upper = [(rel.upper().replace("\\", "/"), size, mtime) for rel, size, mtime in ENTRIES]

    if os.name == "nt":
        assert folder_fingerprint(upper) == folder_fingerprint(ENTRIES)
    else:
        assert folder_fingerprint(upper) != folder_fingerprint(ENTRIES)


# ---------------------------------------------------------------------------
# text_hash and path_hash
# ---------------------------------------------------------------------------

def test_text_hash_is_stable() -> None:
    """Used as a cache key for parsed content; drift would invalidate every entry."""
    assert text_hash("CheckpointLoaderSimple") == text_hash("CheckpointLoaderSimple")


def test_text_hash_is_case_and_whitespace_sensitive() -> None:
    """Unlike paths, text is content: two strings that differ at all are different
    content and must not share a cache entry."""
    assert text_hash("Model") != text_hash("model")
    assert text_hash("a b") != text_hash("ab")
    assert text_hash("") != text_hash(" ")


def test_text_hash_handles_text_that_cannot_be_encoded() -> None:
    """Filenames and metadata read off disk can carry lone surrogates; raising
    here would take down a scan over one bad string."""
    assert len(text_hash("lone \udc80 surrogate")) == 32
    assert len(text_hash("模型 café 🎨")) == 32


def test_text_hash_is_collision_free_over_generated_input() -> None:
    """Node types, workflow titles and metadata values all land in this one
    namespace, so near-identical strings must stay distinct."""
    values = [f"{prefix}_{i}" for prefix in ("node", "Node", "node.type", "node_type")
              for i in range(150)]
    hashes = {text_hash(v) for v in values}

    assert len(values) == 600
    assert len(hashes) == 600


def test_path_hash_is_stable() -> None:
    """Path hashes key thumbnails on disk; an unstable one orphans every cached
    thumbnail on the next run."""
    assert path_hash(PATH) == path_hash(PATH)
    assert path_hash(Path(PATH)) == path_hash(PATH)


def test_path_hash_folds_case_and_separators_on_windows() -> None:
    """One file must have one thumbnail, however the path reached the function.
    On a case-sensitive filesystem these are different files and must differ."""
    variants = [path_hash(PATH), path_hash(PATH.upper()), path_hash(PATH.replace("\\", "/"))]

    if os.name == "nt":
        assert len(set(variants)) == 1
    else:
        assert len(set(variants)) == 3


def test_path_hash_distinguishes_different_files() -> None:
    """Sibling files differing by one character are the norm in a model library."""
    assert path_hash(PATH) != path_hash(PATH.replace("sd15", "sd21"))


def test_path_hash_is_a_plain_storable_string() -> None:
    """The value becomes part of a filename on disk, so it may not contain
    anything a filesystem would reject."""
    fp = path_hash(PATH)

    assert isinstance(fp, str)
    assert set(fp) <= set("0123456789abcdef")
    assert len(fp) == 32


def test_path_hash_is_collision_free_over_a_deep_tree() -> None:
    """Thumbnails are addressed by this hash alone, so a collision would show one
    model's preview under another model's name."""
    paths = [rf"C:\ComfyUI\models\{kind}\vendor_{i:03d}\weights.safetensors"
             for kind in ("checkpoints", "loras", "vae", "controlnet")
             for i in range(150)]
    hashes = {path_hash(p) for p in paths}

    assert len(paths) == 600
    assert len(hashes) == 600


def test_text_hash_and_path_hash_stay_in_separate_namespaces() -> None:
    """They share a digest size and both end up in the same columns; the same
    string hashed as a path and as text must not produce one value, or a lookup by
    the wrong helper would silently succeed."""
    assert path_hash(PATH) != text_hash(PATH)
    assert path_hash(PATH) != text_hash(os.path.normcase(PATH))
