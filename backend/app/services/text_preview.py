"""Read a capped, decoded excerpt of a text-ish file for the preview pane.

The vault holds `.txt` and `.json` outputs worth reading, and `.pt` tensor
files that are PyTorch pickles -- binary, and up to a couple of hundred
megabytes.  Rendering the second kind as "text" would produce a screen of
mojibake, so the decision here is made on the bytes rather than on the
extension: a file is text if a leading sample decodes cleanly and holds no NUL.

Nothing is ever unpickled, parsed, or executed.  JSON is *validated* so the UI
can pretty-print it, and a file that claims `.json` but does not parse is still
returned as plain text rather than rejected.
"""

from __future__ import annotations

import json
import os

#: Never read more than this into memory for a preview.
MAX_BYTES = 512 * 1024
#: Bytes examined when deciding whether the file is text at all.
SNIFF_BYTES = 8192
#: Encodings tried in order; the last one always succeeds with replacement.
ENCODINGS = ("utf-8-sig", "utf-8", "utf-16", "cp1252")

NUL = b"\x00"
TEXT_CONTROLS = ("\t", "\n", "\r")


def _decodes_as_utf8(sample: bytes) -> bool:
    """True if the sample is valid UTF-8, allowing a cut multi-byte tail.

    The sample is a prefix of the file, so it can end part-way through a
    character; dropping up to three trailing bytes stops a perfectly good UTF-8
    file being called binary purely because the cut landed mid-sequence.
    """
    for trim in range(4):
        candidate = sample[: len(sample) - trim] if trim else sample
        try:
            candidate.decode("utf-8")
            return True
        except UnicodeDecodeError:
            continue
    return False


def _looks_binary(sample: bytes) -> bool:
    """A NUL byte is the reliable tell; PyTorch pickles trip it immediately."""
    if NUL in sample:
        return True
    if not sample:
        return False

    # Decode first, and only then fall back to counting bytes.  A ratio of
    # printable ASCII treats UTF-8 as binary -- every accent and emoji is
    # multi-byte and sits above 0x7F, so French prose scores worse than a
    # pickle does.
    if _decodes_as_utf8(sample):
        # Valid UTF-8 can still be a binary container that happens to decode,
        # so reject a sample dominated by control characters.
        text = sample.decode("utf-8", "ignore")
        if not text:
            return False
        control = sum(
            1 for ch in text if ord(ch) < 0x20 and ch not in TEXT_CONTROLS
        )
        return (control / len(text)) > 0.05

    printable = sum(
        1 for b in sample
        if 0x20 <= b < 0x7F or b in (0x09, 0x0A, 0x0D)
    )
    return (printable / len(sample)) < 0.75


def _decode(raw: bytes) -> tuple[str, str]:
    for enc in ENCODINGS[:-1]:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode(ENCODINGS[-1], "replace"), ENCODINGS[-1] + " (with replacements)"


def read_preview(abs_path: str, *, max_bytes: int = MAX_BYTES) -> dict:
    """Return a preview descriptor.  Never raises for an unreadable file."""
    cap = max(1024, min(int(max_bytes or MAX_BYTES), MAX_BYTES))
    try:
        total = os.path.getsize(abs_path)
    except OSError as exc:
        return {"kind": "error", "message": f"Could not stat the file: {exc}"}

    try:
        with open(abs_path, "rb") as fh:
            raw = fh.read(cap + 1)
    except OSError as exc:
        return {"kind": "error", "message": f"Could not read the file: {exc}"}

    truncated = len(raw) > cap
    raw = raw[:cap]

    if _looks_binary(raw[:SNIFF_BYTES]):
        return {
            "kind": "binary",
            "total_bytes": total,
            "message": "This is a binary file, so there is nothing to show as text.",
        }

    text, encoding = _decode(raw)

    parsed_json = False
    if not truncated:
        stripped = text.lstrip()
        if stripped[:1] in ("{", "["):
            try:
                text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
                parsed_json = True
            except (ValueError, RecursionError):
                parsed_json = False

    return {
        "kind": "text",
        "text": text,
        "encoding": encoding,
        "json": parsed_json,
        "truncated": truncated,
        "bytes_read": len(raw),
        "total_bytes": total,
        "lines": text.count("\n") + 1 if text else 0,
    }
