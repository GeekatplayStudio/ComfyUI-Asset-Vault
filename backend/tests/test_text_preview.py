"""Text preview, and the rendered-poster ingest for 3D models.

The interesting decision here is that "is this text?" is answered from the
bytes, not the extension: a ComfyUI output folder holds `.pt` tensor files that
are PyTorch pickles, sometimes hundreds of megabytes, and rendering one as text
would fill the pane with mojibake.
"""

from __future__ import annotations

import base64
import io as _io
import json

import pytest

from app.services import text_preview


def write(tmp_path, name: str, data):
    p = tmp_path / name
    p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return str(p)


# ---------------------------------------------------------------- text kinds

def test_plain_text_is_returned_as_text(tmp_path):
    r = text_preview.read_preview(write(tmp_path, "a.txt", "one\ntwo\nthree\n"))
    assert r["kind"] == "text"
    assert r["lines"] == 4
    assert "two" in r["text"]
    assert r["truncated"] is False


def test_json_is_pretty_printed(tmp_path):
    r = text_preview.read_preview(write(tmp_path, "a.json", '{"b":1,"a":[1,2]}'))
    assert r["kind"] == "text"
    assert r["json"] is True
    assert "\n" in r["text"], "indented JSON spans lines"
    assert json.loads(r["text"]) == {"b": 1, "a": [1, 2]}


def test_json_that_does_not_parse_is_still_shown_as_text(tmp_path):
    """A truncated or hand-edited .json is more useful shown than refused."""
    r = text_preview.read_preview(write(tmp_path, "bad.json", '{"a": 1,,,'))
    assert r["kind"] == "text"
    assert r["json"] is False
    assert '"a"' in r["text"]


# ------------------------------------------------------------------- binary

def test_a_pickle_is_reported_as_binary_not_rendered(tmp_path):
    """`.pt` files are pickles; a NUL byte in the sample is the tell."""
    blob = b"PK\x03\x04" + bytes(range(256)) * 8
    r = text_preview.read_preview(write(tmp_path, "latent.pt", blob))
    assert r["kind"] == "binary"
    assert "text" not in r, "no decoded payload is handed back for binary"
    assert r["total_bytes"] == len(blob)


def test_mostly_unprintable_bytes_count_as_binary(tmp_path):
    r = text_preview.read_preview(write(tmp_path, "x.bin", bytes(range(0x80, 0xFF)) * 40))
    assert r["kind"] == "binary"


def test_utf8_text_with_accents_is_not_mistaken_for_binary(tmp_path):
    r = text_preview.read_preview(write(tmp_path, "u.txt", "café ☕ naïve\n" * 20))
    assert r["kind"] == "text"
    assert "café" in r["text"]


# ------------------------------------------------------------------- limits

def test_a_large_file_is_truncated_rather_than_read_whole(tmp_path):
    big = "x" * (text_preview.MAX_BYTES * 2)
    r = text_preview.read_preview(write(tmp_path, "big.txt", big), max_bytes=4096)
    assert r["kind"] == "text"
    assert r["truncated"] is True
    assert r["bytes_read"] <= 4096
    assert r["total_bytes"] == len(big)


def test_the_cap_is_bounded_even_when_a_caller_asks_for_more(tmp_path):
    big = "y" * (text_preview.MAX_BYTES + 5000)
    r = text_preview.read_preview(write(tmp_path, "b.txt", big),
                                  max_bytes=text_preview.MAX_BYTES * 100)
    assert r["bytes_read"] <= text_preview.MAX_BYTES


def test_a_missing_file_reports_an_error_rather_than_raising(tmp_path):
    r = text_preview.read_preview(str(tmp_path / "nope.txt"))
    assert r["kind"] == "error"


# ------------------------------------------- rendered poster ingest (3D)

def _png_bytes(size: int = 8) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    buf = _io.BytesIO()
    Image.new("RGB", (size, size), (20, 20, 26)).save(buf, format="PNG")
    return buf.getvalue()


def test_a_rendered_poster_must_be_a_png_data_url(temp_vault):
    from app.jobs.thumb_service import get_thumb_service

    svc = get_thumb_service()
    for bad in ("", "not a url", "data:image/gif;base64,AAAA",
                "data:image/png;base64,@@@not-base64@@@"):
        with pytest.raises(ValueError):
            svc.store_rendered("output:1", bad)


def test_a_rendered_poster_is_refused_above_the_size_cap(temp_vault):
    from app.jobs.thumb_service import get_thumb_service

    svc = get_thumb_service()
    huge = base64.b64encode(b"\x89PNG" + b"0" * (svc.MAX_RENDERED_BYTES + 10)).decode()
    with pytest.raises(ValueError) as exc:
        svc.store_rendered("output:1", "data:image/png;base64," + huge)
    assert "larger than" in str(exc.value).lower()


def test_a_rendered_poster_is_refused_for_a_non_3d_asset(temp_vault):
    """The ingest exists for models only; it writes to the thumbnail cache."""
    from app.jobs.thumb_service import get_thumb_service

    svc = get_thumb_service()
    payload = "data:image/png;base64," + base64.b64encode(_png_bytes()).decode()
    with pytest.raises(ValueError):
        svc.store_rendered("output:999999", payload)
