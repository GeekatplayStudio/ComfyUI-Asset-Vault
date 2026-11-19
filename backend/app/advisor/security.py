"""An explainable 0-100 safety score for a model.

What this is: a **risk estimate from published facts** - the file format, who
published it, whether the host's own scanners passed it, how widely it is used,
and whether it asks to run code.  Each fact is one line in ``factors`` with the
points it added or removed, so the number can always be read back.

What this is not: a malware scan.  Nothing here opens the weights.  A score of
95 means "nothing published about this file suggests a problem", never "this
file has been proven clean" - and the payload says so in ``disclaimer``.

The single most important factor is the format.  ``safetensors`` and ``gguf``
are data-only containers: loading one cannot run code.  ``.ckpt``, ``.pt``,
``.pth`` and ``.bin`` are Python pickles, and unpickling an untrusted one can
execute arbitrary code - which is why the indexer in this app reads them with a
disassembler and never loads them.
"""

from __future__ import annotations

import time
from datetime import datetime

SAFE_FORMATS = frozenset({"safetensors", "gguf", "sft", "onnx"})
PICKLE_FORMATS = frozenset({"ckpt", "pt", "pth", "bin", "pkl", "pickle"})

OPEN_LICENSES = frozenset({
    "apache-2.0", "mit", "bsd-3-clause", "bsd-2-clause", "cc0-1.0", "cc-by-4.0",
    "cc-by-sa-4.0", "openrail", "openrail++", "creativeml-openrail-m",
    "bigscience-openrail-m", "unlicense", "isc", "mpl-2.0", "gpl-3.0",
    "lgpl-3.0", "agpl-3.0", "cc-by-2.0", "cc-by-3.0", "wtfpl", "afl-3.0",
})

BASE_SCORE = 40
#: Never 100: the score is an estimate, and the ceiling says so.
MAX_SCORE = 99
DISCLAIMER = ("An estimate from published facts (format, publisher, host scans, "
              "adoption). It is not a malware scan and proves nothing about the "
              "file's contents.")

GRADES = (
    (85, "A", "Safe", "ok"),
    (70, "B", "Low risk", "ok"),
    (50, "C", "Caution", "warn"),
    (30, "D", "Risky", "danger"),
    (0, "F", "Unsafe", "danger"),
)


def grade_for(score: int) -> dict:
    for floor, grade, label, tone in GRADES:
        if score >= floor:
            return {"grade": grade, "label": label, "tone": tone}
    return {"grade": "F", "label": "Unsafe", "tone": "danger"}


def normalize_format(value: str | None) -> str:
    text = str(value or "").strip().lower().lstrip(".")
    return {"safetensor": "safetensors", "pickletensor": "ckpt",
            "pickle tensor": "ckpt", "diffusers": "safetensors"}.get(text, text)


def _age_days(iso: str | None, now: float | None = None) -> float | None:
    if not iso:
        return None
    try:
        stamp = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return max(0.0, ((now or time.time()) - stamp) / 86400.0)


def _factor(factors: list[dict], key: str, label: str, points: int,
            detail: str) -> int:
    tone = "ok" if points > 0 else ("danger" if points <= -15 else
                                    "warn" if points < 0 else "neutral")
    factors.append({"id": key, "label": label, "points": int(points),
                    "detail": detail, "tone": tone})
    return points


def _finish(score: int, factors: list[dict]) -> dict:
    score = max(0, min(MAX_SCORE, int(score)))
    return {"score": score, **grade_for(score), "factors": factors,
            "disclaimer": DISCLAIMER, "inferred": True}


def score_entry(entry: dict, variant: dict | None = None, *,
                now: float | None = None) -> dict:
    """Score a catalog entry, optionally for one specific variant's file."""
    factors: list[dict] = []
    score = BASE_SCORE
    safety = entry.get("safety") or {}
    variants = entry.get("variants") or []

    # --- format -----------------------------------------------------------
    if variant is not None:
        formats = {normalize_format(variant.get("format"))}
    else:
        formats = {normalize_format(v.get("format")) for v in variants}
    formats.discard("")
    pickles = formats & PICKLE_FORMATS
    if formats and not pickles and formats <= SAFE_FORMATS:
        score += _factor(factors, "format", "Data-only format", 25,
                         ", ".join(sorted(formats)) + " cannot execute code when loaded.")
    elif pickles and formats - pickles:
        score += _factor(factors, "format", "Mixed formats", -8,
                         "Some files are pickles (" + ", ".join(sorted(pickles)) +
                         "); prefer the safetensors or GGUF variant.")
    elif pickles:
        score += _factor(factors, "format", "Pickle format", -25,
                         ", ".join(sorted(pickles)) + " files can run code when "
                         "loaded. Only load them from a publisher you trust.")
    else:
        score += _factor(factors, "format", "Format unknown", -5,
                         "No recognised weight file was listed.")

    # --- host scan --------------------------------------------------------
    scan = str(safety.get("scan") or "unknown")
    host = {"huggingface": "Hugging Face", "civitai": "Civitai"}.get(
        str(entry.get("source")), "The host")
    if scan == "clean":
        score += _factor(factors, "scan", "Host scan passed", 10,
                         f"{host} scanned the files and reported no issues.")
    elif scan == "issues":
        score += _factor(factors, "scan", "Host scan flagged files", -45,
                         str(safety.get("scan_detail") or
                             f"{host} flagged one or more files in this repository."))
    elif scan == "pending":
        _factor(factors, "scan", "Host scan pending", 0,
                f"{host} has not finished scanning these files.")
    else:
        _factor(factors, "scan", "No host scan result", 0,
                "This source does not publish a scan result.")

    # --- publisher --------------------------------------------------------
    publisher = str(entry.get("publisher") or "unknown")
    trust = str(safety.get("publisher_trust") or "unknown")
    if trust == "official":
        score += _factor(factors, "publisher", "Original publisher", 15,
                         f"{publisher} is the organisation that created this model.")
    elif trust == "trusted":
        score += _factor(factors, "publisher", "Established repackager", 10,
                         f"{publisher} is a long-standing, widely used packager.")
    elif trust == "curated":
        score += _factor(factors, "publisher", "Curated list entry", 8,
                         "Listed in a maintained, reviewed model list.")
    else:
        _factor(factors, "publisher", "Community publisher", 0,
                f"{publisher} is not on the list of recognised publishers.")

    # --- adoption ---------------------------------------------------------
    downloads = entry.get("downloads")
    if isinstance(downloads, (int, float)):
        if downloads >= 100_000:
            score += _factor(factors, "adoption", "Very widely used", 8,
                             f"{int(downloads):,} downloads.")
        elif downloads >= 10_000:
            score += _factor(factors, "adoption", "Widely used", 5,
                             f"{int(downloads):,} downloads.")
        elif downloads >= 1_000:
            score += _factor(factors, "adoption", "In use", 2,
                             f"{int(downloads):,} downloads.")
        elif downloads < 100:
            score += _factor(factors, "adoption", "Barely used", -5,
                             f"Only {int(downloads):,} downloads - few people "
                             "have had the chance to notice a problem.")

    # --- age --------------------------------------------------------------
    age = _age_days(entry.get("created_at"), now)
    if age is not None:
        if age < 14:
            score += _factor(factors, "age", "Very new", -5,
                             f"Published {int(age)} days ago; not yet widely vetted.")
        elif age >= 90:
            score += _factor(factors, "age", "Established", 3,
                             f"Available for {int(age)} days.")

    # --- code -------------------------------------------------------------
    if safety.get("custom_code"):
        score += _factor(factors, "code", "Requires custom code", -12,
                         "The repository ships code that the loader is asked to "
                         "run. Review it before use.")
    elif int(safety.get("code_files") or 0) > 0:
        score += _factor(factors, "code", "Ships scripts", -3,
                         f"{int(safety['code_files'])} script file(s) sit beside "
                         "the weights. They are not needed to load the model.")

    # --- verifiable -------------------------------------------------------
    if safety.get("hash_published"):
        score += _factor(factors, "hash", "Checksum published", 5,
                         "A SHA-256 is published, so the download can be verified.")

    # --- licence (affects safe *use*, not malware risk) -------------------
    licence = str(entry.get("license") or "").strip().lower()
    if not licence or licence in ("unknown", "none"):
        score += _factor(factors, "license", "No licence stated", -4,
                         "Usage rights are unclear.")
    elif licence in OPEN_LICENSES:
        score += _factor(factors, "license", "Open licence", 4, licence)
    else:
        _factor(factors, "license", "Restricted or custom licence", 0,
                f"{licence} - read the terms before commercial use.")

    return _finish(score, factors)


def score_local(model: dict) -> dict:
    """Score a file already indexed in the vault.

    ``model`` carries the indexer's own findings: ``format``, ``integrity``,
    ``hash_state``, ``civitai_state``, ``download_url``.
    """
    factors: list[dict] = []
    score = BASE_SCORE
    fmt = normalize_format(model.get("format"))
    if fmt in SAFE_FORMATS:
        score += _factor(factors, "format", "Data-only format", 25,
                         f"{fmt} cannot execute code when loaded.")
    elif fmt in PICKLE_FORMATS:
        score += _factor(factors, "format", "Pickle format", -25,
                         f".{fmt} files can run code when loaded. The vault "
                         "reads them without executing anything, but ComfyUI "
                         "will unpickle them.")
    else:
        score += _factor(factors, "format", "Format unknown", -5,
                         "Not a recognised weight container.")

    integrity = str(model.get("integrity") or "ok")
    if integrity == "ok":
        score += _factor(factors, "integrity", "Header reads cleanly", 8,
                         "The file structure is intact.")
    else:
        score += _factor(factors, "integrity", "Integrity problem", -20,
                         f"The indexer reported: {integrity.replace('_', ' ')}.")

    civitai = str(model.get("civitai_state") or "none")
    hashed = str(model.get("hash_state") or "") == "done"
    if civitai == "matched":
        score += _factor(factors, "provenance", "Matches a published file", 15,
                         "Its hash matches a file published on Civitai, so it "
                         "is the file its publisher released.")
    elif civitai == "not_found":
        _factor(factors, "provenance", "Not found upstream", 0,
                "Its hash is not known to Civitai - common for private or "
                "self-trained files.")
    elif hashed:
        score += _factor(factors, "provenance", "Hashed, not yet checked", 2,
                         "A SHA-256 exists; run an online lookup to confirm "
                         "where the file came from.")
    else:
        score += _factor(factors, "provenance", "Origin not verified", -3,
                         "The file has not been hashed, so it cannot be "
                         "matched to a published release.")

    if model.get("download_url"):
        score += _factor(factors, "source", "Source recorded", 2,
                         "The index knows where this file was published.")

    return _finish(score, factors)
