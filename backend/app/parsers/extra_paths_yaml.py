"""Parse ``extra_model_paths.yaml`` with ``yaml.safe_load``.

Absence is normal and silent - the target machine has only ``.example`` and
``.hold``, and neither is read unless the ``read_held_extra_paths`` toggle is on.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..core.pathsafe import long_path, normalize

log = logging.getLogger(__name__)

_IGNORED_KEYS = {"base_path", "is_default", "download_model_base", "config"}


def parse_extra_model_paths(path: str | Path) -> list[tuple[str, Path]]:
    """Return (category, resolved directory) pairs.  Never raises."""
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is a declared dependency
        log.warning("PyYAML is not installed; extra_model_paths.yaml ignored")
        return []
    try:
        with open(long_path(path), "rb") as fh:
            raw = fh.read(4 * 1024 * 1024)
        data = yaml.safe_load(raw.decode("utf-8", "replace"))
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return []
    except Exception as exc:  # noqa: BLE001 - yaml raises its own error family
        log.warning("could not parse %s: %s", path, exc)
        return []

    out: list[tuple[str, Path]] = []
    if not isinstance(data, dict):
        return out
    for body in data.values():
        if not isinstance(body, dict):
            continue
        base = body.get("base_path")
        base_path = normalize(str(base)) if isinstance(base, str) and base.strip() else None
        for key, value in body.items():
            if key in _IGNORED_KEYS or not isinstance(value, str):
                continue
            for entry in value.replace("\r", "").split("\n"):
                entry = entry.strip()
                if not entry:
                    continue
                p = Path(entry)
                if not p.is_absolute() and base_path is not None:
                    p = base_path / entry
                resolved = normalize(p)
                try:
                    if resolved.is_dir():
                        out.append((str(key), resolved))
                except OSError:
                    continue
    # De-duplicate by (category, normcase path).
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, Path]] = []
    for cat, p in out:
        k = (cat, os.path.normcase(str(p)))
        if k not in seen:
            seen.add(k)
            unique.append((cat, p))
    return unique
