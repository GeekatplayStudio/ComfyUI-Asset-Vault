"""Pillow decode budget and format allowlist (SECURITY_REVIEW S-05).

Pillow's default ``MAX_IMAGE_PIXELS`` only warns at 89 Mpx and only refuses at
178 Mpx, so a 324-byte PNG declaring 20000x8000 decoded happily into ~480 MB.
Every file the vault opens is a file it did not write, so the budget is set
explicitly here and imported by both ``Image.open`` owners.

The format allowlist matters for the same reason: without ``formats=`` a byte
sniff can route an ``output/`` file into the PSD, FITS, JPEG2000 or raw-codec
plugins, none of which this application has any use for.  The list is always
filtered through ``Image.OPEN`` before it is passed on, because Pillow raises a
bare ``KeyError`` out of ``Image.open`` for a name it has not registered - so an
unbuilt optional codec such as AVIF would otherwise turn every unrecognised file
into a ``KeyError`` instead of a clean ``UnidentifiedImageError``.
"""

from __future__ import annotations

#: 64 megapixels - four times a 4K frame, far above anything ComfyUI writes.
MAX_IMAGE_PIXELS = 64_000_000

#: Every format the vault's own extension list can legitimately produce.
ALLOWED_FORMATS: tuple[str, ...] = (
    "PNG", "JPEG", "MPO", "WEBP", "GIF", "BMP", "TIFF", "PPM", "AVIF",
)

#: Formats a decoder may emit for a frame handed to us as bytes in memory.
FRAME_FORMATS: tuple[str, ...] = ("PNG", "JPEG", "MPO", "BMP", "PPM")


def apply_budget() -> bool:
    """Pin ``Image.MAX_IMAGE_PIXELS``.  Returns False when Pillow is absent."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        return False
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    return True


_open_formats_cache: dict[tuple[str, ...], list[str]] = {}


def open_formats(names: tuple[str, ...] = ALLOWED_FORMATS) -> list[str]:
    """``names`` narrowed to the plugins this Pillow build actually registered."""
    cached = _open_formats_cache.get(names)
    if cached is not None:
        return list(cached)
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        return []
    Image.init()
    allowed = [n for n in names if n in Image.OPEN]
    _open_formats_cache[names] = allowed
    return list(allowed)


def exceeds_budget(size) -> bool:
    """True when a header's declared dimensions are over the pixel budget.

    Checked from the header, before any ``load()``, so an over-budget file costs
    a header read rather than half a gigabyte of resident memory.
    """
    try:
        width, height = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return False
    return width > 0 and height > 0 and width * height > MAX_IMAGE_PIXELS
