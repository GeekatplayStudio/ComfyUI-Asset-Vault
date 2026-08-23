"""Compose the searchable document for each asset kind.

One definition shared by FTS5 ``body`` and the embedding text, so the lexical
and semantic arms always see the same content.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..core.fingerprint import text_hash

KINDS = ("model", "node_package", "node_class", "workflow", "output")


@dataclass(frozen=True)
class SearchDoc:
    uid: str
    kind: str
    title: str
    subtitle: str
    body: str
    tags: str

    @property
    def text_hash(self) -> str:
        return text_hash(f"{self.title}\x1f{self.subtitle}\x1f{self.body}\x1f{self.tags}")

    @property
    def embed_text(self) -> str:
        parts = [p for p in (self.title, self.subtitle, self.tags, self.body) if p]
        return " · ".join(parts)[:4000]


def _opt(row: sqlite3.Row, key: str):
    """sqlite3.Row has no .get(); this is the safe optional-column accessor."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _json_list(value) -> list:
    if not value:
        return []
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _clean(*parts) -> str:
    out = []
    for p in parts:
        if p is None:
            continue
        s = str(p).strip()
        if s and s.lower() not in ("none", "unknown", "null"):
            out.append(s)
    return " ".join(out)[:8000]


def _params_display(n) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return ""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f}M"
    return ""


def model_doc(row: sqlite3.Row, tags: str = "") -> SearchDoc:
    return SearchDoc(
        uid=f"model:{row['id']}", kind="model",
        title=_clean(row["name"], _opt(row, "filename")),
        subtitle=_clean(row["category"], row["model_role"], row["base_model_family"],
                        row["base_model_variant"], row["precision"],
                        _params_display(row["param_count_primary"])),
        body=_clean(row["architecture_label"], _opt(row, "folder"),
                    _opt(row, "description"),
                    " ".join(str(t) for t in _json_list(
                        _opt(row, "trigger_words_json"))),
                    row["quantization"], row["modality"]),
        tags=tags,
    )


def node_package_doc(row: sqlite3.Row, class_names: str = "", tags: str = "") -> SearchDoc:
    return SearchDoc(
        uid=f"node_package:{row['id']}", kind="node_package",
        title=_clean(row["display_name"], row["folder_name"]),
        subtitle=_clean(row["author"], row["publisher_id"], row["registry_id"]),
        body=_clean(row["description"], row["long_description"], class_names),
        tags=tags,
    )


def node_class_doc(row: sqlite3.Row, package_name: str = "", tags: str = "") -> SearchDoc:
    io_names = ""
    inputs = _opt(row, "input_types_json")
    if inputs:
        try:
            data = json.loads(inputs)
            names = []
            for block in ("required", "optional"):
                sub = data.get(block)
                if isinstance(sub, dict):
                    names.extend(list(sub)[:24])
            io_names = " ".join(names)
        except (ValueError, TypeError, AttributeError):
            io_names = ""
    return SearchDoc(
        uid=f"node_class:{row['id']}", kind="node_class",
        title=_clean(row["display_name"], row["node_id"]),
        subtitle=_clean(row["category"], package_name),
        body=_clean(row["description"], row["class_name"], io_names,
                    " ".join(str(t) for t in _json_list(row["return_types_json"]))),
        tags=tags,
    )


def workflow_doc(row: sqlite3.Row, node_list: str = "", tags: str = "") -> SearchDoc:
    return SearchDoc(
        uid=f"workflow:{row['id']}", kind="workflow",
        title=_clean(row["title"], row["name"]),
        subtitle=_clean(row["folder"], row["base_model_family"], row["modality"]),
        body=_clean(row["description"], row["prompt_summary"], row["positive_prompt"],
                    node_list,
                    " ".join(str(t) for t in _json_list(row["capability_tags_json"]))),
        tags=tags,
    )


def output_doc(row: sqlite3.Row, tags: str = "") -> SearchDoc:
    return SearchDoc(
        uid=f"output:{row['id']}", kind="output",
        title=_clean(row["filename"]),
        subtitle=_clean(row["folder"], row["media_kind"], row["model_name"]),
        body=_clean(row["positive_prompt"], row["negative_prompt"], row["sampler"],
                    row["scheduler"]),
        tags=tags,
    )


BUILDERS = {
    "model": model_doc,
    "node_package": node_package_doc,
    "node_class": node_class_doc,
    "workflow": workflow_doc,
    "output": output_doc,
}
