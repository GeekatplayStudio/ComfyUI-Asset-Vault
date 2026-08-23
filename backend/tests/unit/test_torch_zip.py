"""The pickle security boundary: checkpoints are disassembled, never unpickled.

``.ckpt``/``.pt`` files are pickles, and unpickling one runs whatever the author
put in it.  The vault indexes files the user downloaded from strangers, so the
reader is built on ``pickletools.genops``, which walks the opcode stream as data
and never dispatches ``REDUCE`` or resolves ``GLOBAL``.  The central test here
builds a genuinely weaponized checkpoint - a pickle whose opcodes spell out a
call to ``os.system`` - parses it, and then proves the command never ran.

The rest of the file is the same contract the other parsers carry: containers
that are truncated, empty, or not zips at all must come back as results, because
one raised exception inside the scan loop costs the user the rest of the folder.
"""

from __future__ import annotations

import io
import os
import pickle
import pickletools
import sys
import zipfile
from pathlib import Path

import pytest

from app.core import errors
from app.parsers import torch_zip as tz

# A plausible state dict.  Real checkpoints store torch storage objects here; the
# reader only ever sees the strings in the opcode stream, so metadata stand-ins
# exercise exactly the same code path at a fraction of the size.
STATE = {
    "model.diffusion_model.input_blocks.0.0.weight": {"dtype": "torch.float16",
                                                      "shape": [320, 4, 3, 3]},
    "model.diffusion_model.input_blocks.0.0.bias": {"dtype": "torch.float16",
                                                    "shape": [320]},
    "model.diffusion_model.middle_block.1.norm.weight": {"dtype": "torch.float16",
                                                         "shape": [1280]},
    "first_stage_model.encoder.conv_in.weight": {"dtype": "torch.float32",
                                                 "shape": [128, 3, 3, 3]},
    "epoch": 42,
    "global_step": 87000,
}

MARKER_NAME = "breach_marker_do_not_create"


class ReduceBomb:
    """Serializes to opcodes that would spawn a shell command on unpickling.

    ``__reduce__`` is the documented pickle extension point and the whole reason
    ``torch.load`` on an untrusted file is remote code execution.  Nothing here is
    unusual or obfuscated - it is the payload every real malicious checkpoint uses.
    """

    def __init__(self, command: str) -> None:
        self.command = command

    def __reduce__(self):
        return (os.system, (self.command,))


def _breach_command(marker: Path) -> str:
    return f'echo breach > "{marker}"'


def _zip(path: Path, members: dict[str, bytes], *,
         compress: int = zipfile.ZIP_STORED) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=compress) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _checkpoint(path: Path, payload: object, *, protocol: int = 2) -> Path:
    """A container shaped exactly like ``torch.save``'s zip format."""
    return _zip(path, {
        "archive/data.pkl": pickle.dumps(payload, protocol=protocol),
        "archive/version": b"3\n",
        "archive/data/0": b"\0" * 64,
        "archive/data/1": b"\0" * 64,
    })


# ---------------------------------------------------------------------------
# Reading a well-formed checkpoint
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("protocol", [2, 4, 5])
def test_zip_checkpoint_yields_its_tensor_keys(tmp_path: Path, protocol: int) -> None:
    """Torch has emitted several pickle protocols over the years and a library
    holds files from all of them; each encodes strings with a different opcode, so
    the extractor has to recognise the whole family."""
    res = tz.read_keys(_checkpoint(tmp_path / "model.ckpt", STATE, protocol=protocol))

    assert res.ok is True
    assert res.fmt == "torch_zip"
    assert res.error_code is None
    assert res.integrity == "ok"
    assert set(res.keys) == {
        "model.diffusion_model.input_blocks.0.0.weight",
        "model.diffusion_model.input_blocks.0.0.bias",
        "model.diffusion_model.middle_block.1.norm.weight",
        "first_stage_model.encoder.conv_in.weight",
    }
    assert res.tensor_count == 4


def test_bookkeeping_scalars_are_not_mistaken_for_tensors(tmp_path: Path) -> None:
    """Training checkpoints carry ``epoch`` and ``global_step`` beside the weights;
    counting those as tensors would inflate every parameter estimate."""
    res = tz.read_keys(_checkpoint(tmp_path / "model.ckpt", STATE))

    assert "epoch" not in res.keys
    assert "global_step" not in res.keys


def test_raw_strings_are_kept_for_the_detectors(tmp_path: Path) -> None:
    """Architecture detection also reads non-key strings - dtype names, module
    paths - so the disassembled strings are returned alongside the filtered keys."""
    res = tz.read_keys(_checkpoint(tmp_path / "model.ckpt", STATE))

    assert "torch.float16" in res.strings
    assert len(res.strings) >= len(res.keys)
    assert res.file_size == (tmp_path / "model.ckpt").stat().st_size


def test_pkl_member_under_another_name_is_still_found(tmp_path: Path) -> None:
    """Not every producer names the archive folder ``archive`` or the pickle
    ``data.pkl``; falling back to any ``.pkl`` member keeps those readable."""
    res = tz.read_keys(_zip(tmp_path / "odd.ckpt", {
        "some_model/weights.pkl": pickle.dumps(STATE, protocol=2),
    }))

    assert res.ok is True
    assert res.keys


# ---------------------------------------------------------------------------
# The security boundary
# ---------------------------------------------------------------------------

def test_malicious_pickle_is_read_without_executing(tmp_path: Path, monkeypatch) -> None:
    """The whole reason this parser exists.

    The payload is a real ``__reduce__`` bomb: unpickling it would shell out and
    create the marker file.  Parsing must leave that file absent.  ``os.system``
    is replaced with a tripwire as well, because a marker file could in principle
    fail to appear for an unrelated reason and the test would then pass while the
    process really had been hijacked.
    """
    marker = tmp_path / MARKER_NAME
    payload = pickle.dumps(ReduceBomb(_breach_command(marker)), protocol=2)
    path = _zip(tmp_path / "trojan.ckpt", {"archive/data.pkl": payload})

    fired: list[str] = []

    def tripwire(command):
        fired.append(command)
        return 0

    # The opcode stream names the module ``os.system`` was defined in, not ``os``
    # itself, so the tripwire has to cover both - and the lookup has to happen
    # before the first patch replaces the function it reads.
    host = sys.modules.get(os.system.__module__)
    monkeypatch.setattr(os, "system", tripwire)
    if host is not None:
        monkeypatch.setattr(host, "system", tripwire, raising=False)

    res = tz.read_keys(path)

    assert fired == []
    assert not marker.exists()
    assert list(tmp_path.glob(f"**/{MARKER_NAME}*")) == []
    assert res.ok is False
    assert res.integrity == "unsupported_format"


def test_malicious_payload_is_visible_as_inert_data(tmp_path: Path) -> None:
    """Proof the parser really did walk the hostile opcodes rather than skipping
    the file: the command string comes back among the extracted strings, which can
    only happen if the stream was disassembled - and it still never ran."""
    marker = tmp_path / MARKER_NAME
    command = _breach_command(marker)
    payload = pickle.dumps(ReduceBomb(command), protocol=2)
    path = _zip(tmp_path / "trojan.ckpt", {"archive/data.pkl": payload})

    res = tz.read_keys(path)

    assert command in res.strings
    assert not marker.exists()


def test_reduce_bomb_hidden_inside_a_real_state_dict(tmp_path: Path) -> None:
    """The realistic attack is not a bare payload, it is a working checkpoint with
    one poisoned value.  The reader must still recover the model's keys from it
    while leaving the payload inert - refusing the file outright would push the
    user toward loading it in torch instead."""
    marker = tmp_path / MARKER_NAME
    poisoned = dict(STATE)
    poisoned["state_dict_hook"] = ReduceBomb(_breach_command(marker))
    path = _checkpoint(tmp_path / "poisoned.ckpt", poisoned)

    res = tz.read_keys(path)

    assert not marker.exists()
    assert res.ok is True
    assert "model.diffusion_model.input_blocks.0.0.weight" in res.keys


def test_reader_never_calls_the_pickle_module(tmp_path: Path, monkeypatch) -> None:
    """A future refactor could reach for ``pickle.load`` for the awkward cases and
    reintroduce arbitrary code execution behind a passing key-extraction test, so
    the module's own entry points are trapped."""
    path = _checkpoint(tmp_path / "model.ckpt", STATE)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the checkpoint reader unpickled its input")

    monkeypatch.setattr(pickle, "load", forbidden)
    monkeypatch.setattr(pickle, "loads", forbidden)
    monkeypatch.setattr(pickle, "Unpickler", forbidden)

    res = tz.read_keys(path)

    assert res.ok is True


def test_disassembly_matches_the_opcodes_on_disk(tmp_path: Path) -> None:
    """Pins the mechanism itself: the bomb's bytes really do contain a REDUCE
    against a global, which is what makes the non-execution above meaningful."""
    payload = pickle.dumps(ReduceBomb("echo nothing"), protocol=2)
    opcodes = {op.name for op, _arg, _pos in pickletools.genops(io.BytesIO(payload))}

    assert "REDUCE" in opcodes
    assert "GLOBAL" in opcodes or "STACK_GLOBAL" in opcodes


# ---------------------------------------------------------------------------
# Broken containers: a result, never an exception
# ---------------------------------------------------------------------------

def test_non_zip_container_is_reported(tmp_path: Path) -> None:
    """Pre-1.6 torch wrote a bare pickle with no zip around it.  Its keys are only
    reachable by unpickling, which is exactly what this parser will not do, so it
    has to decline the file instead of guessing."""
    path = tmp_path / "legacy.pt"
    path.write_bytes(pickle.dumps(STATE, protocol=2))

    res = tz.read_keys(path)

    assert res.ok is False
    assert res.fmt == "torch_legacy"
    assert res.integrity == "unsupported_format"
    assert "unpickling" in (res.integrity_note or "")


def test_arbitrary_non_zip_bytes_are_reported(tmp_path: Path) -> None:
    """Anything can end up with a ``.ckpt`` extension, including text."""
    path = tmp_path / "notes.ckpt"
    path.write_bytes(b"this is not a checkpoint at all\n" * 40)

    res = tz.read_keys(path)

    assert res.ok is False
    assert res.integrity != "ok"


def test_empty_zip_is_reported(tmp_path: Path) -> None:
    """A zero-member archive is still a valid zip, so the missing pickle is only
    caught after the container opens."""
    res = tz.read_keys(_zip(tmp_path / "empty.ckpt", {}))

    assert res.ok is False
    assert res.fmt == "torch_zip"
    assert res.integrity == "unsupported_format"
    assert "data.pkl" in (res.integrity_note or "")


def test_zip_without_a_pickle_is_reported(tmp_path: Path) -> None:
    """Some archives carry only the tensor storages, for instance when a save was
    interrupted before the index was written."""
    res = tz.read_keys(_zip(tmp_path / "nopkl.ckpt", {
        "archive/version": b"3\n",
        "archive/data/0": b"\0" * 32,
    }))

    assert res.ok is False
    assert res.integrity == "unsupported_format"
    assert "data.pkl" in (res.integrity_note or "")


def test_truncated_zip_is_reported(tmp_path: Path) -> None:
    """A download cut off mid-archive loses its central directory; the zip module
    raises for that, and the raise must not escape."""
    whole = _checkpoint(tmp_path / "full.ckpt", STATE).read_bytes()
    path = tmp_path / "cut.ckpt"
    path.write_bytes(whole[: len(whole) // 2])

    res = tz.read_keys(path)

    assert res.ok is False
    assert res.integrity != "ok"


def test_corrupt_pickle_stops_early_without_raising(tmp_path: Path) -> None:
    """``genops`` raises on an unknown opcode partway through a damaged stream.
    Whatever was recovered before that point is still worth keeping, and the
    exception is data about the file rather than a bug in the reader."""
    res = tz.read_keys(_zip(tmp_path / "corrupt.ckpt", {
        "archive/data.pkl": b"\x80\x05\xff\xff\xff not a pickle",
    }))

    assert res.ok is False
    assert "stopped early" in (res.integrity_note or "")


def test_oversized_pickle_is_refused_before_reading(tmp_path: Path) -> None:
    """A zip bomb declares a huge uncompressed member behind a few kilobytes of
    deflate.  Deciding on the declared size keeps the reader from materialising it
    in memory."""
    path = _zip(tmp_path / "bomb.ckpt",
                {"archive/data.pkl": b"\0" * (tz.MAX_PICKLE + 1024)},
                compress=zipfile.ZIP_DEFLATED)

    assert path.stat().st_size < tz.MAX_PICKLE

    res = tz.read_keys(path)

    assert res.ok is False
    assert res.integrity == "unsupported_format"
    assert "refusing to scan" in (res.integrity_note or "")


def test_pickle_with_no_tensor_keys_is_not_ok(tmp_path: Path) -> None:
    """A readable pickle holding no weights is not a model; marking it ok would
    put an empty row in the index."""
    res = tz.read_keys(_checkpoint(tmp_path / "config.ckpt",
                                   {"learning_rate": 0.0001, "notes": "nothing here"}))

    assert res.ok is False
    assert res.integrity == "unsupported_format"


def test_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    """Files vanish between the directory walk and the read; that race is ordinary
    and must not abort the enclosing scan."""
    res = tz.read_keys(tmp_path / "gone.ckpt")

    assert res.ok is False
    assert res.integrity == "unreadable"
    assert res.error_code in errors.SCAN_ERROR_CODES


def test_every_broken_container_returns_a_result(tmp_path: Path) -> None:
    """One sweep over the corpus, because the contract the scanner depends on is
    not per-case: nothing raises, and nothing comes back claiming to be ok."""
    fixtures = [
        _zip(tmp_path / "a.ckpt", {}),
        _zip(tmp_path / "b.ckpt", {"archive/version": b"3\n"}),
        _zip(tmp_path / "c.ckpt", {"archive/data.pkl": b""}),
        _zip(tmp_path / "d.ckpt", {"archive/data.pkl": b"\xff\xfe\xfd"}),
        tmp_path / "e-missing.ckpt",
    ]
    fixtures.append(tmp_path / "f.ckpt")
    fixtures[-1].write_bytes(b"PK\x03\x04 truncated local header")

    for path in fixtures:
        res = tz.read_keys(path)
        assert res.ok is False, path
        assert res.integrity != "ok", path


# ---------------------------------------------------------------------------
# Container sniffing
# ---------------------------------------------------------------------------

def test_is_zip_distinguishes_the_two_checkpoint_formats(tmp_path: Path) -> None:
    """The format sniff picks which reader runs at all, so a wrong answer sends a
    modern checkpoint down the legacy path where its keys are unreachable."""
    modern = _checkpoint(tmp_path / "modern.ckpt", STATE)
    legacy = tmp_path / "legacy.pt"
    legacy.write_bytes(pickle.dumps(STATE, protocol=2))

    assert tz.is_zip(modern) is True
    assert tz.is_zip(legacy) is False
    assert tz.is_zip(tmp_path / "absent.ckpt") is False
