"""Static node-class extraction, six strategies (fixes B4).

**Absolute rule: never ``import``, never ``exec``, never ``eval``, never
``subprocess`` a package's code.**  Everything here is ``ast.parse`` over source
text - the module is never executed.

S1  literal ``NODE_CLASS_MAPPINGS = {...}`` in any .py under the package
S2  augmenting assignment: ``.update(X)``, ``{**A, **B}``, ``dict(A, **B)``, ``|=``
S3  re-export: ``from .mod import NODE_CLASS_MAPPINGS``
S4  V3 schema: ``Schema(node_id=...)`` / ``IO.Schema(node_id=...)``
S5  structural class scan: ``INPUT_TYPES`` + one of RETURN_TYPES/FUNCTION/CATEGORY
S6  registry enrichment (node_registry.py) - no code read at all
"""

from __future__ import annotations

import ast
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from ..core import errors
from ..core.pathsafe import long_path
from ..indexing.walker import is_reparse_point

PRUNE_DIRS = {
    "__pycache__", ".git", ".github", "node_modules", "web", "js", "dist", "build",
    "tests", "test", ".venv", "venv", "site-packages", ".idea", ".vscode", "docs",
    "example_workflows", "workflows", "examples", "assets", "images", "static",
    ".ruff_cache", ".pytest_cache", "locales", "third_party", "vendor",
    "custom_mmpkg", "custom_detectron2", "custom_pycocotools", "custom_albumentations",
    "custom_oneformer", "custom_midas_repo", "custom_controlnet_aux",
}
MAX_PY_BYTES = 4 * 1024 * 1024
MAX_FILES = 4000
MAX_REEXPORT_DEPTH = 4

MAPPING_NAMES = ("NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS")
SCHEMA_CALLS = ("Schema", "SchemaV3", "SchemaV1")
STRUCT_MARKERS = {"RETURN_TYPES", "FUNCTION", "CATEGORY", "OUTPUT_NODE", "RETURN_NAMES"}

# Cheap byte-level prefilter: a module that mentions none of these cannot
# register a node, so parsing it would be wasted work.  This is what keeps a
# 666-file vendored package (comfyui_controlnet_aux) inside the time budget.
RELEVANT_MARKERS = ("NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS",
                    "INPUT_TYPES", "Schema(", "SchemaV3(", "ComfyNode")


@dataclass
class NodeClass:
    node_id: str
    class_name: str | None = None
    display_name: str | None = None
    category: str | None = None
    description: str | None = None
    input_types: dict | None = None
    return_types: list | None = None
    return_names: list | None = None
    output_node: bool = False
    function_name: str | None = None
    is_deprecated: bool = False
    is_experimental: bool = False
    is_api_node: bool = False
    source_file: str | None = None
    source_lineno: int | None = None
    strategies: set[str] = field(default_factory=set)

    @property
    def confidence(self) -> str:
        if self.strategies & {"S1", "S2", "S3", "S4"}:
            return "declared"
        if "S5" in self.strategies:
            return "inferred"
        return "registry"

    @property
    def primary_strategy(self) -> str:
        for s in ("S1", "S2", "S3", "S4", "S5", "S6"):
            if s in self.strategies:
                return s
        return "S6"

    def merge(self, other: NodeClass) -> None:
        self.strategies |= other.strategies
        for attr in ("class_name", "display_name", "category", "description",
                     "input_types", "return_types", "return_names",
                     "function_name", "source_file", "source_lineno"):
            if getattr(self, attr) in (None, "", [], {}) and getattr(other, attr) is not None:
                setattr(self, attr, getattr(other, attr))
        self.output_node = self.output_node or other.output_node
        self.is_deprecated = self.is_deprecated or other.is_deprecated
        self.is_experimental = self.is_experimental or other.is_experimental
        self.is_api_node = self.is_api_node or other.is_api_node


@dataclass
class ExtractResult:
    classes: dict[str, NodeClass] = field(default_factory=dict)
    strategies: set[str] = field(default_factory=set)
    files_scanned: int = 0
    py_files: int = 0
    errors: list[tuple[str, str, str]] = field(default_factory=list)  # (path, code, message)
    source_breakdown: dict[str, int] = field(default_factory=dict)

    def add(self, nc: NodeClass, strategy: str, source: str | None = None) -> None:
        nc.strategies.add(strategy)
        self.strategies.add(strategy)
        if source:
            self.source_breakdown[source] = self.source_breakdown.get(source, 0) + 1
        existing = self.classes.get(nc.node_id)
        if existing is None:
            self.classes[nc.node_id] = nc
        else:
            existing.merge(nc)


# ---------------------------------------------------------------------------
# Source loading
# ---------------------------------------------------------------------------

def read_source(path: str | Path) -> str | None:
    p = long_path(path)
    try:
        if os.path.getsize(p) > MAX_PY_BYTES:
            return None
        with open(p, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def parse_source(path: str | Path, *, require_markers: bool = False) -> ast.Module | None:
    src = read_source(path)
    if src is None:
        return None
    if require_markers and not any(m in src for m in RELEVANT_MARKERS):
        return None
    with warnings.catch_warnings():
        # Vendored third-party sources emit SyntaxWarning for stale escapes.
        warnings.simplefilter("ignore")
        try:
            return ast.parse(src, filename=str(path))
        except (SyntaxError, ValueError, MemoryError, RecursionError):
            return None


def walk_python_files(root: Path, limit: int = MAX_FILES,
                      on_skip=None) -> list[Path]:
    """Every .py under a package.  Never descends a reparse point (S-01).

    A custom_nodes package is third-party content by definition, so a junction
    shipped inside one previously let the AST scanner read and index Python from
    anywhere on the filesystem.
    """
    out: list[Path] = []
    stack = [root]
    while stack and len(out) < limit:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            try:
                if is_reparse_point(e):
                    if on_skip is not None:
                        on_skip(e.path, "reparse_point")
                    continue
                if e.is_dir(follow_symlinks=False):
                    if e.name in PRUNE_DIRS or e.name.startswith("."):
                        continue
                    stack.append(Path(e.path))
                elif e.name.endswith(".py"):
                    out.append(Path(e.path))
                    if len(out) >= limit:
                        break
            except OSError:
                continue
    return out


# ---------------------------------------------------------------------------
# Literal helpers
# ---------------------------------------------------------------------------

def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [v.value for v in node.values
                 if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        return "".join(parts) if parts else None
    return None


def _const_bool(node: ast.AST) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _literal(node: ast.AST):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def _class_ref_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _class_ref_name(node.func)
    return None


# ---------------------------------------------------------------------------
# S1 / S2 - mapping assignments in one module
# ---------------------------------------------------------------------------

def _dict_entries(node: ast.AST) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Return ((node_id, class_name) pairs, referenced Names) for a dict expr."""
    pairs: list[tuple[str, str | None]] = []
    refs: list[str] = []
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values, strict=False):
            if k is None:  # {**OTHER}
                name = _class_ref_name(v)
                if name:
                    refs.append(name)
                continue
            key = _const_str(k)
            if key:
                pairs.append((key, _class_ref_name(v)))
    elif isinstance(node, ast.Call):
        fname = _class_ref_name(node.func)
        if fname == "dict":
            for arg in node.args:
                p2, r2 = _dict_entries(arg)
                pairs += p2
                refs += r2
            for kw in node.keywords:
                if kw.arg is None:
                    name = _class_ref_name(kw.value)
                    if name:
                        refs.append(name)
                else:
                    pairs.append((kw.arg, _class_ref_name(kw.value)))
        else:
            name = _class_ref_name(node)
            if name:
                refs.append(name)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        for side in (node.left, node.right):
            p2, r2 = _dict_entries(side)
            pairs += p2
            refs += r2
    elif isinstance(node, (ast.Name, ast.Attribute)):
        name = _class_ref_name(node)
        if name:
            refs.append(name)
    return pairs, refs


def collect_mappings(tree: ast.Module) -> tuple[dict[str, str | None], dict[str, str], list[str]]:
    """Return (node_id -> class name, node_id -> display name, unresolved refs)."""
    mapping: dict[str, str | None] = {}
    display: dict[str, str] = {}
    refs: list[str] = []
    # Local dict variables so `X = {...}` then `NODE_CLASS_MAPPINGS = X` resolves.
    locals_: dict[str, tuple[list[tuple[str, str | None]], list[str]]] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(node.value, ast.Dict):
                    locals_.setdefault(target.id, _dict_entries(node.value))
        elif (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and isinstance(node.value, ast.Dict)):
            locals_.setdefault(node.target.id, _dict_entries(node.value))

    def expand(name: str, depth: int = 0) -> list[tuple[str, str | None]]:
        if depth > 4 or name not in locals_:
            return []
        pairs, sub = locals_[name]
        out = list(pairs)
        for s in sub:
            out += expand(s, depth + 1)
        return out

    def absorb(target_name: str, value: ast.AST) -> None:
        pairs, sub = _dict_entries(value)
        if target_name == "NODE_CLASS_MAPPINGS":
            for key, cls in pairs:
                mapping.setdefault(key, cls)
        for ref in sub:
            expanded = expand(ref)
            if expanded:
                for key, cls in expanded:
                    if target_name == "NODE_CLASS_MAPPINGS":
                        mapping.setdefault(key, cls)
            else:
                refs.append(ref)

    for node in ast.walk(tree):
        # NODE_CLASS_MAPPINGS = {...} / X / A | B / dict(...)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                name = _class_ref_name(target)
                if name in MAPPING_NAMES:
                    if name == "NODE_DISPLAY_NAME_MAPPINGS":
                        lit = _literal(node.value)
                        if isinstance(lit, dict):
                            display.update({k: v for k, v in lit.items()
                                            if isinstance(k, str) and isinstance(v, str)})
                        continue
                    absorb(name, node.value)
        # NODE_CLASS_MAPPINGS |= X
        elif isinstance(node, ast.AugAssign):
            name = _class_ref_name(node.target)
            if name == "NODE_CLASS_MAPPINGS" and isinstance(node.op, ast.BitOr):
                absorb(name, node.value)
        # NODE_CLASS_MAPPINGS.update(X)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "update" and _class_ref_name(node.func.value) == "NODE_CLASS_MAPPINGS":
                for arg in node.args:
                    absorb("NODE_CLASS_MAPPINGS", arg)
                mapping.update({kw.arg: _class_ref_name(kw.value)
                                for kw in node.keywords
                                if kw.arg and kw.arg not in mapping})
        # NODE_CLASS_MAPPINGS["X"] = Y
        elif isinstance(node, ast.Assign) is False:
            continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if (isinstance(tgt, ast.Subscript)
                    and _class_ref_name(tgt.value) == "NODE_CLASS_MAPPINGS"):
                key = _const_str(tgt.slice)
                if key:
                    mapping.setdefault(key, _class_ref_name(node.value))
            if (isinstance(tgt, ast.Subscript)
                    and _class_ref_name(tgt.value) == "NODE_DISPLAY_NAME_MAPPINGS"):
                key = _const_str(tgt.slice)
                val = _const_str(node.value)
                if key and val:
                    display[key] = val
    return mapping, display, refs


def collect_imports(tree: ast.Module) -> tuple[dict[str, tuple[str, int]], list[tuple[str, int]]]:
    """name -> (module, level) plus the modules that re-export the mappings."""
    names: dict[str, tuple[str, int]] = {}
    reexports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            level = node.level or 0
            for alias in node.names:
                if alias.name == "*":
                    reexports.append((mod, level))
                    continue
                names[alias.asname or alias.name] = (mod, level)
                if alias.name in MAPPING_NAMES:
                    reexports.append((mod, level))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.split(".")[0]] = (alias.name, 0)
    return names, reexports


# ---------------------------------------------------------------------------
# S4 - V3 schema
# ---------------------------------------------------------------------------

def collect_v3_schemas(tree: ast.Module, rel: str) -> list[NodeClass]:
    out: list[NodeClass] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = node.func.attr if isinstance(node.func, ast.Attribute) else (
            node.func.id if isinstance(node.func, ast.Name) else None)
        if fname not in SCHEMA_CALLS:
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        node_id = _const_str(kw.get("node_id")) if "node_id" in kw else None
        if not node_id:
            continue
        nc = NodeClass(
            node_id=node_id,
            display_name=_const_str(kw["display_name"]) if "display_name" in kw else None,
            category=_const_str(kw["category"]) if "category" in kw else None,
            description=_const_str(kw["description"]) if "description" in kw else None,
            is_deprecated=bool(_const_bool(kw.get("is_deprecated"))) if "is_deprecated" in kw else False,
            is_experimental=bool(_const_bool(kw.get("is_experimental"))) if "is_experimental" in kw else False,
            is_api_node=bool(_const_bool(kw.get("is_api_node"))) if "is_api_node" in kw else False,
            source_file=rel,
            source_lineno=getattr(node, "lineno", None),
        )
        outs = kw.get("outputs")
        if isinstance(outs, (ast.List, ast.Tuple)):
            types = []
            for el in outs.elts:
                nm = _class_ref_name(el)
                if nm:
                    types.append(nm.replace("Output", "").upper() or nm)
            nc.return_types = types or None
        inputs = kw.get("inputs")
        if isinstance(inputs, (ast.List, ast.Tuple)):
            req: dict[str, str] = {}
            opt: dict[str, str] = {}
            for el in inputs.elts:
                if not isinstance(el, ast.Call):
                    continue
                iname = _const_str(el.args[0]) if el.args else None
                if not iname:
                    continue
                tname = _class_ref_name(el.func) or "ANY"
                tname = tname.replace(".Input", "")
                optional = any(k.arg == "optional" and _const_bool(k.value)
                               for k in el.keywords)
                (opt if optional else req)[iname] = tname.upper()
            if req or opt:
                nc.input_types = {"required": req, "optional": opt}
        # Enclosing class name, when the schema sits inside a ComfyNode subclass.
        out.append(nc)
    _attach_enclosing_class(tree, out)
    return out


def _attach_enclosing_class(tree: ast.Module, classes: list[NodeClass]) -> None:
    if not classes:
        return
    by_line = {c.source_lineno: c for c in classes if c.source_lineno}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", start)
        for line, nc in by_line.items():
            if start <= line <= end and nc.class_name is None:
                nc.class_name = node.name



# ---------------------------------------------------------------------------
# S5 - structural class scan
# ---------------------------------------------------------------------------

def _input_types_from_class(cls: ast.ClassDef) -> dict | None:
    for item in cls.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if item.name != "INPUT_TYPES":
            continue
        for sub in ast.walk(item):
            if isinstance(sub, ast.Return) and sub.value is not None:
                lit = _literal(sub.value)
                if isinstance(lit, dict):
                    return _simplify_input_types(lit)
                if isinstance(sub.value, ast.Dict):
                    return _simplify_input_dict(sub.value)
        return None
    return None


def _type_label(v) -> str:
    if isinstance(v, (list, tuple)) and v:
        head = v[0]
        if isinstance(head, str):
            return head
        if isinstance(head, (list, tuple)):
            return "COMBO"
        return "ANY"
    if isinstance(v, str):
        return v
    return "ANY"


def _simplify_input_types(lit: dict) -> dict:
    out: dict = {}
    for section in ("required", "optional", "hidden"):
        block = lit.get(section)
        if isinstance(block, dict):
            out[section] = {str(k): _type_label(v) for k, v in list(block.items())[:64]}
    return out or {"required": {}}


def _simplify_input_dict(node: ast.Dict) -> dict:
    out: dict = {}
    for k, v in zip(node.keys, node.values, strict=False):
        key = _const_str(k) if k is not None else None
        if key not in ("required", "optional", "hidden") or not isinstance(v, ast.Dict):
            continue
        block: dict[str, str] = {}
        for k2, v2 in zip(v.keys, v.values, strict=False):
            name = _const_str(k2) if k2 is not None else None
            if not name:
                continue
            lit = _literal(v2)
            if lit is not None:
                block[name] = _type_label(lit)
            elif isinstance(v2, (ast.Tuple, ast.List)) and v2.elts:
                s = _const_str(v2.elts[0])
                block[name] = s if s else "COMBO"
            else:
                block[name] = "ANY"
        out[key] = block
    return out


def collect_structural(tree: ast.Module, rel: str) -> list[NodeClass]:
    out: list[NodeClass] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        methods = {m.name for m in node.body
                   if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
        attrs: dict[str, ast.AST] = {}
        for m in node.body:
            if isinstance(m, ast.Assign):
                for t in m.targets:
                    if isinstance(t, ast.Name):
                        attrs[t.id] = m.value
            elif (isinstance(m, ast.AnnAssign) and isinstance(m.target, ast.Name)
                    and m.value is not None):
                attrs[m.target.id] = m.value
        if "INPUT_TYPES" not in methods or not (STRUCT_MARKERS & set(attrs)):
            continue
        nc = NodeClass(node_id=node.name, class_name=node.name, source_file=rel,
                       source_lineno=node.lineno)
        rt = _literal(attrs["RETURN_TYPES"]) if "RETURN_TYPES" in attrs else None
        if isinstance(rt, (list, tuple)):
            nc.return_types = [str(x) for x in rt][:32]
        rn = _literal(attrs["RETURN_NAMES"]) if "RETURN_NAMES" in attrs else None
        if isinstance(rn, (list, tuple)):
            nc.return_names = [str(x) for x in rn][:32]
        nc.category = _const_str(attrs["CATEGORY"]) if "CATEGORY" in attrs else None
        nc.function_name = _const_str(attrs["FUNCTION"]) if "FUNCTION" in attrs else None
        nc.output_node = bool(_const_bool(attrs.get("OUTPUT_NODE"))) if "OUTPUT_NODE" in attrs else False
        nc.description = _const_str(attrs["DESCRIPTION"]) if "DESCRIPTION" in attrs else None
        nc.is_deprecated = bool(_const_bool(attrs.get("DEPRECATED"))) if "DEPRECATED" in attrs else False
        nc.is_experimental = bool(_const_bool(attrs.get("EXPERIMENTAL"))) if "EXPERIMENTAL" in attrs else False
        nc.is_api_node = bool(_const_bool(attrs.get("API_NODE"))) if "API_NODE" in attrs else False
        if nc.description is None:
            doc = ast.get_docstring(node)
            if doc:
                nc.description = doc.strip()[:2000]
        nc.input_types = _input_types_from_class(node)
        out.append(nc)
    return out


# ---------------------------------------------------------------------------
# Module resolution for S3
# ---------------------------------------------------------------------------

def _resolve_module(base_dir: Path, module: str, level: int, pkg_root: Path) -> Path | None:
    """Resolve a relative/absolute import to a file inside the package."""
    if level and level > 0:
        start = base_dir
        for _ in range(level - 1):
            start = start.parent
    else:
        start = pkg_root
    parts = module.split(".") if module else []
    candidate = start
    for part in parts:
        if not part:
            continue
        candidate = candidate / part
    for probe in (candidate.with_suffix(".py"), candidate / "__init__.py"):
        try:
            if probe.is_file():
                return probe
        except OSError:
            continue
    # ``src/`` layouts
    for extra in ("src", "py", "nodes", "modules"):
        alt = start / extra
        cand = alt
        for part in parts:
            cand = cand / part
        for probe in (cand.with_suffix(".py"), cand / "__init__.py"):
            try:
                if probe.is_file():
                    return probe
            except OSError:
                continue
    return None


# ---------------------------------------------------------------------------
# Package extraction
# ---------------------------------------------------------------------------

def extract_package(pkg_dir: Path, *, pkg_root: Path | None = None,
                    files: list[Path] | None = None,
                    label_source: bool = False) -> ExtractResult:
    """Run S1-S5 over one package directory."""
    res = ExtractResult()
    pkg_root = pkg_root or pkg_dir
    if files is None:
        files = walk_python_files(pkg_dir)
    res.py_files = len(files)

    trees: dict[Path, ast.Module] = {}
    for f in files:
        src = read_source(f)
        if src is None or not any(m in src for m in RELEVANT_MARKERS):
            continue
        tree = parse_source(f)
        if tree is None:
            res.errors.append((str(f), errors.AST_SYNTAX_ERROR,
                               "Module declares node markers but could not be parsed."))
            continue
        trees[f] = tree
        res.files_scanned += 1

    # --- S1 / S2 / S4 / S5 over every module --------------------------------
    display_names: dict[str, str] = {}
    struct_by_name: dict[str, NodeClass] = {}
    for f, tree in trees.items():
        rel = os.path.relpath(str(f), str(pkg_root))
        source_label = rel if label_source else None
        mapping, display, refs = collect_mappings(tree)
        display_names.update(display)
        for node_id, cls_name in mapping.items():
            strategy = "S1" if not refs else "S2"
            res.add(NodeClass(node_id=node_id, class_name=cls_name, source_file=rel),
                    strategy, source_label)
        for nc in collect_v3_schemas(tree, rel):
            res.add(nc, "S4", source_label)
        for nc in collect_structural(tree, rel):
            struct_by_name[nc.class_name or nc.node_id] = nc

    # --- S3: follow re-exports outside the walked set ------------------------
    seen_files = set(trees)
    queue: list[tuple[Path, int]] = []
    for f, tree in list(trees.items()):
        _names, reexports = collect_imports(tree)
        for mod, level in reexports:
            target = _resolve_module(f.parent, mod, level, pkg_root)
            if target and target not in seen_files:
                queue.append((target, 1))
    while queue:
        target, depth = queue.pop()
        if depth > MAX_REEXPORT_DEPTH or target in seen_files:
            continue
        seen_files.add(target)
        tree = parse_source(target)
        if tree is None:
            continue
        res.files_scanned += 1
        rel = os.path.relpath(str(target), str(pkg_root))
        mapping, display, _refs = collect_mappings(tree)
        display_names.update(display)
        for node_id, cls_name in mapping.items():
            res.add(NodeClass(node_id=node_id, class_name=cls_name, source_file=rel), "S3")
        for nc in collect_v3_schemas(tree, rel):
            res.add(nc, "S4")
        for nc in collect_structural(tree, rel):
            struct_by_name.setdefault(nc.class_name or nc.node_id, nc)
        _n2, reexports = collect_imports(tree)
        for mod, level in reexports:
            nxt = _resolve_module(target.parent, mod, level, pkg_root)
            if nxt and nxt not in seen_files:
                queue.append((nxt, depth + 1))

    # --- merge S5 -----------------------------------------------------------
    declared_by_class = {nc.class_name: nc for nc in res.classes.values() if nc.class_name}
    for cls_name, nc in struct_by_name.items():
        target = declared_by_class.get(cls_name)
        if target is not None:
            # Enrich without changing confidence.
            target.merge(NodeClass(
                node_id=target.node_id, class_name=cls_name,
                display_name=nc.display_name, category=nc.category,
                description=nc.description, input_types=nc.input_types,
                return_types=nc.return_types, return_names=nc.return_names,
                output_node=nc.output_node, function_name=nc.function_name,
                is_deprecated=nc.is_deprecated, is_experimental=nc.is_experimental,
                is_api_node=nc.is_api_node,
                source_file=nc.source_file, source_lineno=nc.source_lineno,
            ))
            target.strategies.add("S5")
            res.strategies.add("S5")
        elif cls_name not in res.classes:
            res.add(nc, "S5")

    # --- display names ------------------------------------------------------
    for node_id, disp in display_names.items():
        nc = res.classes.get(node_id)
        if nc is not None and not nc.display_name:
            nc.display_name = disp
    for nc in res.classes.values():
        if not nc.display_name:
            nc.display_name = nc.node_id
    return res


def extract_official(comfy_root: Path) -> ExtractResult:
    """Index ComfyUI's own node classes as the synthetic ``__comfyui_core__``."""
    files: list[Path] = []
    nodes_py = comfy_root / "nodes.py"
    if nodes_py.is_file():
        files.append(nodes_py)
    for sub in ("comfy_extras", "comfy_api_nodes"):
        d = comfy_root / sub
        if d.is_dir():
            files.extend(sorted(p for p in d.rglob("*.py")
                                if not any(part in PRUNE_DIRS for part in p.parts)))
    res = extract_package(comfy_root, pkg_root=comfy_root, files=files, label_source=False)
    # Label the source breakdown the way the API contract expects.
    breakdown: dict[str, int] = {}
    for nc in res.classes.values():
        sf = (nc.source_file or "").replace("\\", "/")
        if sf.startswith("comfy_api_nodes"):
            key = "comfy_api_nodes"
        elif sf.startswith("comfy_extras"):
            key = "comfy_extras (V3 schema)" if "S4" in nc.strategies else "comfy_extras (legacy)"
        elif sf.startswith("nodes.py"):
            key = "nodes.py"
        else:
            key = "other"
        breakdown[key] = breakdown.get(key, 0) + 1
        if sf.startswith("comfy_api_nodes"):
            nc.is_api_node = True
    res.source_breakdown = breakdown
    return res
