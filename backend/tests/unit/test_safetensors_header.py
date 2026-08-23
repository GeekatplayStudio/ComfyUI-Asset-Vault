"""Layer-0 header reading: the parser sees the whole key set, and it never raises.

The scanner walks a library that contains files nobody validated: half-finished
downloads, HTML error pages saved under a ``.safetensors`` name, Git-LFS pointer
stubs, and files a hostile uploader shaped on purpose.  Every one of those has to
come back as a *result* carrying a stable error code, because a raised exception
inside the scan loop aborts the whole folder and the user loses the rest of their
library from the index.  These tests therefore assert two things about every
malformed input: no exception escaped, and the code that came back is one the API
contract already knows how to render.

The second theme is completeness.  A truncated view of the tensor keys is what
made architecture detection pick the wrong family, so the valid-file tests assert
the *full* key set, not a prefix of it.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from conftest import write_safetensors

from app.core import errors
from app.parsers import safetensors_header as sh

# Files below the parser's MIN_FILE floor are rejected on size alone, so every
# fixture is padded past it; otherwise a test would pass for the wrong reason.
PADDED = sh.MIN_FILE * 2


def _pad8(blob: bytes) -> bytes:
    """Safetensors headers are 8-byte aligned; spaces are legal JSON whitespace."""
    return blob + b" " * ((8 - len(blob) % 8) % 8)


def _write_raw(path: Path, payload: bytes, *, size: int = PADDED) -> Path:
    """Write ``payload`` verbatim and zero-fill to ``size``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload + b"\0" * max(0, size - len(payload)))
    return path


def _container(path: Path, header: bytes, *, declared: int | None = None) -> Path:
    """Assemble a safetensors container from a header blob and a declared length.

    ``declared`` defaults to the true length; passing a different value is how the
    hostile fixtures claim a header the file cannot possibly hold.
    """
    blob = _pad8(header)
    n = len(blob) if declared is None else declared
    return _write_raw(path, struct.pack("<Q", n) + blob)


def _json_container(path: Path, header: dict, *, declared: int | None = None) -> Path:
    return _container(path, json.dumps(header).encode(), declared=declared)


GOOD_HEADER = {
    "model.diffusion_model.input_blocks.0.0.weight": {
        "dtype": "F16", "shape": [320, 4, 3, 3], "data_offsets": [0, 23040]},
    "model.diffusion_model.input_blocks.0.0.bias": {
        "dtype": "F16", "shape": [320], "data_offsets": [23040, 23680]},
    "first_stage_model.encoder.conv_in.weight": {
        "dtype": "F32", "shape": [128, 3, 3, 3], "data_offsets": [23680, 37504]},
    "__metadata__": {"format": "pt", "ss_network_dim": "32"},
}


# ---------------------------------------------------------------------------
# A header that is actually valid
# ---------------------------------------------------------------------------

def test_valid_header_yields_every_key(tmp_path: Path) -> None:
    """Key order in a safetensors header is arbitrary, so a partial read silently
    drops the very keys that identify the architecture.  The parser must return
    the complete set."""
    res = sh.read_header(_json_container(tmp_path / "m.safetensors", GOOD_HEADER))

    assert res.ok is True
    assert res.error_code is None
    assert res.integrity == "ok"
    assert res.keys == [
        "model.diffusion_model.input_blocks.0.0.weight",
        "model.diffusion_model.input_blocks.0.0.bias",
        "first_stage_model.encoder.conv_in.weight",
    ]
    assert res.tensor_count == 3
    assert "__metadata__" not in res.keys


def test_valid_header_yields_shapes_and_dtypes(tmp_path: Path) -> None:
    """Shapes drive the parameter count and dtypes drive the precision label; both
    are read straight off the header rather than inferred from the file size."""
    res = sh.read_header(_json_container(tmp_path / "m.safetensors", GOOD_HEADER))

    assert res.shapes["model.diffusion_model.input_blocks.0.0.weight"] == [320, 4, 3, 3]
    assert res.shapes["model.diffusion_model.input_blocks.0.0.bias"] == [320]
    assert res.dtypes["first_stage_model.encoder.conv_in.weight"] == "F32"
    assert res.dtype_of("model.diffusion_model.input_blocks.0.0.weight") == "F16"
    assert res.dtype_of("not-a-key") is None
    # 320*4*3*3 + 320 + 128*3*3*3 == 11520 + 320 + 3456
    assert res.param_total == 15296


def test_valid_header_preserves_metadata_verbatim(tmp_path: Path) -> None:
    """Trainer metadata is the only provenance many LoRAs carry; the reader must
    hand it back untouched rather than normalizing or dropping unknown keys."""
    res = sh.read_header(_json_container(tmp_path / "m.safetensors", GOOD_HEADER))

    assert res.metadata == {"format": "pt", "ss_network_dim": "32"}


def test_header_without_metadata_reports_an_empty_dict(tmp_path: Path) -> None:
    """Callers index ``metadata`` directly, so its absence must be an empty dict
    and never ``None``."""
    header = {k: v for k, v in GOOD_HEADER.items() if k != "__metadata__"}
    res = sh.read_header(_json_container(tmp_path / "m.safetensors", header))

    assert res.ok is True
    assert res.metadata == {}


def test_non_dict_metadata_is_ignored(tmp_path: Path) -> None:
    """A writer that stores ``__metadata__`` as a list must not poison the record
    for the tensors, which are still perfectly readable."""
    header = dict(GOOD_HEADER, __metadata__=["not", "a", "dict"])
    res = sh.read_header(_json_container(tmp_path / "m.safetensors", header))

    assert res.ok is True
    assert res.metadata == {}
    assert res.tensor_count == 3


def test_shared_fixture_builder_produces_a_parseable_file(tmp_path: Path) -> None:
    """Most of the suite trusts ``write_safetensors`` to stand in for a real model;
    if that builder drifted from the format, those tests would go green on files
    the production reader cannot actually read."""
    path = write_safetensors(
        tmp_path / "lora.safetensors",
        {"lora_unet_blocks_0.lora_down.weight": ("F16", (32, 320)),
         "lora_unet_blocks_0.lora_up.weight": ("F16", (320, 32))},
        metadata={"ss_network_dim": 32},
    )
    res = sh.read_header(path)

    assert res.ok is True
    assert len(res.keys) == 2
    assert res.metadata["ss_network_dim"] == "32"
    assert res.file_size == path.stat().st_size


def test_reported_sizes_match_the_file_on_disk(tmp_path: Path) -> None:
    """``header_bytes`` and ``file_size`` are stored and later used to decide
    whether a file needs rescanning, so they must describe the real bytes."""
    path = _json_container(tmp_path / "m.safetensors", GOOD_HEADER)
    res = sh.read_header(path)

    assert res.file_size == path.stat().st_size
    assert res.header_bytes == len(_pad8(json.dumps(GOOD_HEADER).encode()))


def test_explicit_file_size_is_trusted_over_a_stat_call(tmp_path: Path) -> None:
    """The scanner already holds the size from its directory walk; passing it in
    must skip the redundant stat rather than being ignored."""
    path = _json_container(tmp_path / "m.safetensors", GOOD_HEADER)
    res = sh.read_header(path, file_size=path.stat().st_size)

    assert res.ok is True
    assert res.file_size == path.stat().st_size


# ---------------------------------------------------------------------------
# Malformed input: a result with a code, never an exception
# ---------------------------------------------------------------------------

def test_header_larger_than_the_cap_is_refused(tmp_path: Path) -> None:
    """An attacker-supplied length field is a memory-exhaustion primitive: the
    reader would allocate whatever it is told.  The cap must turn that into a
    reported code, not an allocation and not a raise."""
    path = _container(tmp_path / "huge.safetensors", b"", declared=sh.MAX_HEADER + 1)
    res = sh.read_header(path)

    assert res.ok is False
    assert res.error_code == errors.HEADER_TOO_LARGE
    assert res.integrity == "invalid_header"
    assert res.header_bytes == sh.MAX_HEADER + 1


def test_zero_byte_file_is_reported_not_raised(tmp_path: Path) -> None:
    """An interrupted download leaves a zero-byte placeholder; it must be recorded
    as a non-model so the row survives and the user can see why."""
    path = _write_raw(tmp_path / "empty.safetensors", b"", size=0)
    res = sh.read_header(path)

    assert res.ok is False
    assert res.error_code == errors.NOT_A_MODEL
    assert res.integrity == "truncated"
    assert res.file_size == 0


def test_truncated_file_is_reported(tmp_path: Path) -> None:
    """A partial download keeps a plausible length field but not the bytes behind
    it; the mismatch has to be caught before the JSON decoder sees it."""
    header = {f"blocks.{i}.weight": {"dtype": "F16", "shape": [64, 64],
                                     "data_offsets": [0, 8192]}
              for i in range(400)}
    blob = _pad8(json.dumps(header).encode())
    whole = struct.pack("<Q", len(blob)) + blob + b"\0" * 65536
    path = tmp_path / "partial.safetensors"
    path.write_bytes(whole[:10_000])

    res = sh.read_header(path)

    assert res.ok is False
    assert res.error_code == errors.HEADER_INVALID
    assert res.integrity == "invalid_header"


def test_header_shorter_than_declared_is_reported(tmp_path: Path) -> None:
    """The scanner passes the size it recorded during its walk.  If the file was
    truncated between the walk and the read, the short read must be caught rather
    than parsed as if the missing bytes were empty."""
    blob = _pad8(json.dumps(GOOD_HEADER).encode())
    path = tmp_path / "shrunk.safetensors"
    path.write_bytes(struct.pack("<Q", len(blob)) + blob[:-16])

    res = sh.read_header(path, file_size=PADDED * 4)

    assert res.ok is False
    assert res.error_code == errors.HEADER_INVALID
    assert res.integrity == "truncated"


def test_garbage_leading_bytes_are_reported(tmp_path: Path) -> None:
    """Random bytes decode as an absurd length; that path must reach the same
    reported outcome as any other unreadable header."""
    path = _write_raw(tmp_path / "noise.safetensors",
                      b"\xde\xad\xbe\xef\xca\xfe\xba\xbe" + b"\xff" * 256)
    res = sh.read_header(path)

    assert res.ok is False
    assert res.error_code in (errors.HEADER_INVALID, errors.HEADER_TOO_LARGE)
    assert res.error_code in errors.SCAN_ERROR_CODES


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("html_error_page", b"<!DOCTYPE html><html><body>404 Not Found</body></html>"),
        ("lowercase_html", b"<html><head><title>Forbidden</title></head></html>"),
        ("xml_error", b"<?xml version=\"1.0\"?><Error><Code>AccessDenied</Code></Error>"),
        ("git_lfs_pointer",
         b"version https://git-lfs.github.com/spec/v1\noid sha256:0123456789ab\nsize 42\n"),
    ],
)
def test_downloaded_text_masquerading_as_a_model(tmp_path: Path, label: str,
                                                 payload: bytes) -> None:
    """A failed download saves the error body under the model's name at the model's
    size.  Calling that NOT_A_MODEL rather than a header failure is what lets the
    UI tell the user to download the file again."""
    path = _write_raw(tmp_path / f"{label}.safetensors", payload)
    res = sh.read_header(path)

    assert res.ok is False
    assert res.error_code == errors.NOT_A_MODEL
    assert res.integrity == "not_a_model"
    assert res.integrity_note


def test_declared_header_overruns_the_file(tmp_path: Path) -> None:
    """A length that is under the cap but past the end of the file is the cheap
    version of the same attack, and must not turn into a short read that parses."""
    path = _json_container(tmp_path / "overrun.safetensors", GOOD_HEADER,
                           declared=5_000_000)
    res = sh.read_header(path)

    assert res.ok is False
    assert res.error_code == errors.HEADER_INVALID
    assert "5000000" in (res.integrity_note or "")


@pytest.mark.parametrize("declared", [0, 1])
def test_impossibly_small_declared_header(tmp_path: Path, declared: int) -> None:
    """A header cannot be shorter than ``{}``; accepting one would hand the JSON
    decoder an empty slice and report a confusing decode failure instead."""
    path = _container(tmp_path / "tiny.safetensors", b"", declared=declared)
    res = sh.read_header(path)

    assert res.ok is False
    assert res.error_code == errors.HEADER_INVALID


def test_non_utf8_header_bytes_are_reported_as_an_encoding_error(tmp_path: Path) -> None:
    """Safetensors headers are UTF-8 by specification.  A UTF-16 or otherwise
    mis-encoded header gets its own code so the user is told the file is corrupt
    rather than merely unsupported."""
    path = _container(tmp_path / "utf16.safetensors",
                      b"\xff\xfe{\x00\"\x00a\x00\"\x00:\x001\x00}\x00")
    res = sh.read_header(path)

    assert res.ok is False
    assert res.error_code == errors.ENCODING_ERROR
    assert res.integrity == "invalid_header"


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("array", b"[1, 2, 3]"),
        ("string", b"\"just a string\""),
        ("number", b"1234567"),
        ("null", b"null"),
        ("bool", b"true"),
    ],
)
def test_valid_json_that_is_not_an_object(tmp_path: Path, label: str,
                                          payload: bytes) -> None:
    """``json.loads`` happily returns a list or a scalar, and the very next line
    would call ``.items()`` on it.  The type check has to come first."""
    path = _container(tmp_path / f"{label}.safetensors", payload)
    res = sh.read_header(path)

    assert res.ok is False
    assert res.error_code == errors.HEADER_INVALID
    assert res.keys == []


def test_malformed_json_is_reported(tmp_path: Path) -> None:
    """A header cut mid-object still has a self-consistent length field, so the
    decode failure is the only signal that the file is damaged."""
    path = _container(tmp_path / "bad.safetensors", b"{\"a\": {\"dtype\": \"F16\",")
    res = sh.read_header(path)

    assert res.ok is False
    assert res.error_code == errors.HEADER_INVALID


@pytest.mark.parametrize("header", [{}, {"__metadata__": {"format": "pt"}}])
def test_header_declaring_no_tensors_is_not_ok(tmp_path: Path, header: dict) -> None:
    """A model with no tensors is not a model.  Accepting it would create an index
    row that every downstream detector then has to special-case."""
    path = _json_container(tmp_path / "notensors.safetensors", header)
    res = sh.read_header(path)

    assert res.ok is False
    assert res.error_code == errors.HEADER_INVALID
    assert res.tensor_count == 0


def test_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    """Files disappear between the directory walk and the read; that race is
    ordinary and must not abort the enclosing scan."""
    res = sh.read_header(tmp_path / "gone.safetensors")

    assert res.ok is False
    assert res.integrity == "unreadable"
    assert res.error_code in errors.SCAN_ERROR_CODES


def test_directory_passed_as_a_file_is_reported(tmp_path: Path) -> None:
    """A path that resolves to a directory reaches the reader whenever a folder is
    named like a model; opening it raises OSError deep inside."""
    target = tmp_path / "model.safetensors"
    target.mkdir()

    res = sh.read_header(target)

    assert res.ok is False
    assert res.error_code in errors.SCAN_ERROR_CODES


def test_every_hostile_fixture_returns_a_known_code(tmp_path: Path) -> None:
    """One sweep over the whole corpus, because the contract the scanner relies on
    is not per-case: *nothing* raises, and every failure carries a code the API
    already knows how to render."""
    fixtures = [
        _write_raw(tmp_path / "a.safetensors", b"", size=0),
        _write_raw(tmp_path / "b.safetensors", b"\x00" * 7, size=7),
        _write_raw(tmp_path / "c.safetensors", b"<!DOCTYPE html>"),
        _write_raw(tmp_path / "d.safetensors", b"\xff" * 512),
        _container(tmp_path / "e.safetensors", b"", declared=sh.MAX_HEADER + 1),
        _container(tmp_path / "f.safetensors", b"[]"),
        _container(tmp_path / "g.safetensors", b"\xc3\x28\xa0\xa1"),
        _json_container(tmp_path / "h.safetensors", GOOD_HEADER, declared=2**40),
        tmp_path / "i-does-not-exist.safetensors",
    ]

    for path in fixtures:
        res = sh.read_header(path)
        assert res.ok is False, path
        assert res.error_code in errors.SCAN_ERROR_CODES, path
        assert res.integrity != "ok", path


# ---------------------------------------------------------------------------
# Precision summary
# ---------------------------------------------------------------------------

def test_dominant_precision_of_an_empty_header() -> None:
    """A header with no dtypes must yield no claim at all rather than a default
    that would later be displayed as fact."""
    assert sh.dominant_precision({}, {}) == (None, None)


def test_dominant_precision_of_a_uniform_model() -> None:
    """The common case: one dtype throughout, and nothing to disambiguate."""
    precision, quant = sh.dominant_precision(
        {"a.weight": "F16", "b.weight": "F16"},
        {"a.weight": [4096, 4096], "b.weight": [4096]},
    )
    assert precision == "fp16"
    assert quant is None


def test_dominant_precision_is_weighted_by_parameter_count() -> None:
    """Counting tensors instead of parameters lets a handful of tiny fp32 bias
    vectors outvote a multi-gigabyte fp16 body and mislabel the whole model."""
    precision, _ = sh.dominant_precision(
        {"body.weight": "F16", **{f"bias.{i}.bias": "F32" for i in range(20)}},
        {"body.weight": [8192, 8192], **{f"bias.{i}.bias": [8] for i in range(20)}},
    )
    assert precision == "fp16"


@pytest.mark.parametrize(
    ("dtype", "precision", "quant"),
    [
        ("F8_E4M3", "fp8", "fp8_e4m3"),
        ("F8_E5M2", "fp8", "fp8_e5m2"),
        ("I8", "int8", "int8"),
        ("U8", "uint8", "int8"),
        ("NF4", "nf4", "nf4"),
        ("F4", "fp4", "f4"),
    ],
)
def test_quantized_body_keeps_its_label_beside_fp16_norms(dtype: str, precision: str,
                                                          quant: str) -> None:
    """Every real quantized checkpoint keeps its norms and embeddings at higher
    precision.  Calling that "mixed" would hide the one fact a user filtering for
    quantized models actually needs."""
    got_precision, got_quant = sh.dominant_precision(
        {"body.weight": dtype, "norm.weight": "F16"},
        {"body.weight": [8192, 8192], "norm.weight": [8192]},
    )
    assert got_precision == precision
    assert got_quant == quant


def test_three_unquantized_precisions_report_mixed() -> None:
    """A bundled checkpoint carrying fp32, fp16 and bf16 blocks has no single
    honest headline precision, so it must say so instead of picking one."""
    precision, quant = sh.dominant_precision(
        {"a.weight": "F32", "b.weight": "F16", "c.weight": "BF16"},
        {"a.weight": [4096, 4096], "b.weight": [512], "c.weight": [512]},
    )
    assert precision == "mixed"
    assert quant is None


def test_two_unquantized_precisions_report_the_heavier_one() -> None:
    """fp16 weights beside an fp32 head are still an fp16 model; "mixed" is only
    earned once no single precision dominates."""
    precision, quant = sh.dominant_precision(
        {"a.weight": "F16", "b.weight": "F32"},
        {"a.weight": [4096, 4096], "b.weight": [512]},
    )
    assert precision == "fp16"
    assert quant is None


def test_unknown_dtype_is_passed_through_lowercased() -> None:
    """New dtypes ship faster than this table does; an unrecognised one must be
    reported as itself rather than swallowed or crashed on."""
    precision, quant = sh.dominant_precision({"a.weight": "F6_E3M2"},
                                             {"a.weight": [64, 64]})
    assert precision == "f6_e3m2"
    assert quant is None


def test_dominant_precision_survives_missing_shapes() -> None:
    """Shapes and dtypes come from independent header fields, so either can be
    absent for a given key without taking the summary down with it."""
    precision, _ = sh.dominant_precision({"a.weight": "BF16"}, {})
    assert precision == "bf16"


# ---------------------------------------------------------------------------
# Metadata serialization
# ---------------------------------------------------------------------------

def test_metadata_json_returns_none_for_nothing_to_store() -> None:
    """An empty block must not become the string ``"{}"`` in the database, where
    it would read as "this model carries metadata"."""
    assert sh.metadata_json({}) is None


def test_metadata_json_round_trips_a_small_block() -> None:
    """Small metadata is stored verbatim so the original values remain searchable."""
    out = sh.metadata_json({"ss_network_dim": "32", "modelspec.title": "Café ☕"})

    assert json.loads(out) == {"ss_network_dim": "32", "modelspec.title": "Café ☕"}


def test_metadata_json_is_capped() -> None:
    """Some trainers embed the entire training config, including datasets lists
    megabytes long; storing that verbatim bloats every query that touches the row."""
    out = sh.metadata_json({f"key{i}": "x" * 4000 for i in range(60)})

    assert out is not None
    assert len(out) <= sh.METADATA_CAP
    assert json.loads(out)["__truncated__"] is True
