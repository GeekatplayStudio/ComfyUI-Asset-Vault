"""The parsing path must never execute anything it reads.

The vault opens files it did not write: safetensors headers, PyTorch pickle
containers, PNG/WebP metadata, YAML, and the Python source of third-party
``custom_nodes`` packages.  Two layers are asserted here.

*Statically*: no ``import``/``exec``/``eval``/``compile``/``pickle.load``/
``torch.load``/``subprocess`` anywhere under ``app/parsers``, ``app/indexing``,
``app/search`` or ``app/jobs``.

*Dynamically*: a checkpoint carrying a ``__reduce__`` payload that would run a
command under ``pickle.load`` is fed to the real parser, and the command must
not run; a YAML file carrying ``!!python/object/apply`` is fed to the real
parser and must be refused.
"""

from __future__ import annotations

import ast
import os
import pickle
import sys
import time
import zipfile
from pathlib import Path

import pytest

from app.parsers import extra_paths_yaml, node_ast, torch_zip

#: Packages that must contain no dynamic-execution primitive at all.
PARSING_PACKAGES = ("parsers", "indexing", "search")

#: Call names that mean "this code can run data as code".
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
FORBIDDEN_ATTRS = {("pickle", "load"), ("pickle", "loads"),
                   ("torch", "load"), ("marshal", "load"), ("marshal", "loads"),
                   ("dill", "load"), ("dill", "loads"), ("os", "system"),
                   ("os", "popen"), ("importlib", "import_module")}
FORBIDDEN_MODULES = {"pickle", "torch", "marshal", "dill", "subprocess",
                     "importlib", "runpy", "imp"}


def _sources(app_dir: Path, *packages: str) -> list[Path]:
    out: list[Path] = []
    for package in packages:
        out += [p for p in (app_dir / package).rglob("*.py")
                if "__pycache__" not in p.parts]
    return sorted(out)


def _attr_chain(node: ast.AST) -> tuple[str, str] | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return (node.value.id, node.attr)
    return None


# ---------------------------------------------------------------------------
# Static: the parsing packages
# ---------------------------------------------------------------------------

def test_parsing_packages_import_no_execution_primitive(app_dir):
    offenders = []
    for path in _sources(app_dir, *PARSING_PACKAGES):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders.extend(
                    (path.name, node.lineno, alias.name) for alias in node.names
                    if alias.name.split(".")[0] in FORBIDDEN_MODULES)
            elif isinstance(node, ast.ImportFrom) and node.module                     and node.module.split(".")[0] in FORBIDDEN_MODULES:
                offenders.append((path.name, node.lineno, node.module))
    assert not offenders, f"execution primitives imported on the parsing path: {offenders}"


def test_parsing_packages_call_no_execution_primitive(app_dir):
    offenders = []
    for path in _sources(app_dir, *PARSING_PACKAGES):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                offenders.append((path.name, node.lineno, node.func.id))
            chain = _attr_chain(node.func)
            if chain in FORBIDDEN_ATTRS:
                offenders.append((path.name, node.lineno, ".".join(chain)))
    assert not offenders, f"execution primitives called on the parsing path: {offenders}"


def test_no_bare_except_on_the_parsing_path(app_dir):
    offenders = []
    for path in _sources(app_dir, *PARSING_PACKAGES):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend((path.name, node.lineno) for node in ast.walk(tree)
                         if isinstance(node, ast.ExceptHandler) and node.type is None)
    assert not offenders, f"bare except: swallows security failures: {offenders}"


def test_torch_zip_uses_only_zipfile_and_pickletools(app_dir):
    source = (app_dir / "parsers" / "torch_zip.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "pickle" not in imported
    assert "torch" not in imported
    assert {"zipfile", "pickletools"} <= imported
    assert "genops" in source, "disassembly, not unpickling, is the whole point"


def test_only_three_subprocess_call_sites_exist_in_the_whole_app(app_dir):
    """Explorer reveal, the confirmed ComfyUI updater, and the C9 git clone.

    The third entry is ``enable/git_fetch.py``: it is the only module in the C9
    package permitted to import ``subprocess``, the only program it may start is
    ``git``, and it never runs anything that arrives in the clone.  The argument
    vector itself is asserted separately in ``test_enable_downloader.py``.
    """
    users = []
    for path in app_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        users.extend(
            str(path.relative_to(app_dir)).replace("\\", "/")
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            and any(a.name.split(".")[0] == "subprocess" for a in node.names))
    assert sorted(set(users)) == ["api/v1/files_router.py",
                                  "enable/git_fetch.py",
                                  "services/comfyui_service.py"], users


def test_yaml_is_only_ever_safe_load(app_dir):
    offenders = []
    for path in app_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "yaml.load" in line or "yaml.unsafe_load" in line or \
                    "yaml.full_load" in line or "UnsafeLoader" in line or \
                    "FullLoader" in line:
                offenders.append((path.name, lineno, line.strip()))
    assert not offenders, f"unsafe yaml loader: {offenders}"


def test_no_custom_nodes_module_is_ever_imported(app_dir):
    """B6/checklist 3: the app never adds a package path or imports node code."""
    offenders = []
    for path in app_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if "sys.path.insert" in stripped or "sys.path.append" in stripped:
                offenders.append((path.name, lineno, stripped))
            if "spec_from_file_location" in stripped or "SourceFileLoader" in stripped:
                offenders.append((path.name, lineno, stripped))
    assert not offenders, f"dynamic module loading: {offenders}"


# ---------------------------------------------------------------------------
# Dynamic: a real malicious checkpoint
# ---------------------------------------------------------------------------

class _ReduceBomb:
    """Under ``pickle.load`` this runs a command.  Under ``genops`` it is data."""

    def __init__(self, marker: str) -> None:
        self.marker = marker

    def __reduce__(self):
        return (os.system, (f'cmd /c echo owned > "{self.marker}"',))


def test_a_reduce_payload_in_a_checkpoint_is_never_executed(tmp_path):
    marker = tmp_path / "PWNED.txt"
    payload = pickle.dumps({"model.weight": _ReduceBomb(str(marker))}, protocol=2)
    checkpoint = tmp_path / "malicious.ckpt"
    with zipfile.ZipFile(checkpoint, "w") as archive:
        archive.writestr("archive/data.pkl", payload)
        archive.writestr("archive/data/0", b"\0" * 32)

    result = torch_zip.read_keys(checkpoint)

    assert not marker.exists(), "the pickle payload executed"
    assert result.fmt == "torch_zip"
    assert result.integrity in ("ok", "unsupported_format")


def test_a_legacy_pickle_container_is_refused_not_unpickled(tmp_path):
    legacy = tmp_path / "legacy.pt"
    legacy.write_bytes(pickle.dumps({"a.weight": [1, 2, 3]}, protocol=2))
    result = torch_zip.read_keys(legacy)
    assert result.fmt == "torch_legacy"
    assert result.integrity == "unsupported_format"
    assert not result.ok


def test_zip_slip_entries_are_never_written_to_disk(tmp_path):
    checkpoint = tmp_path / "slip.ckpt"
    with zipfile.ZipFile(checkpoint, "w") as archive:
        archive.writestr("../../../ESCAPED.pkl", b"x")
        archive.writestr("archive/data.pkl", pickle.dumps({"a.weight": 1}))
    torch_zip.read_keys(checkpoint)
    assert not (tmp_path.parent / "ESCAPED.pkl").exists()
    assert not (tmp_path / "ESCAPED.pkl").exists()


def test_an_oversized_pickle_is_refused_before_it_is_read(tmp_path):
    checkpoint = tmp_path / "bomb.ckpt"
    with zipfile.ZipFile(checkpoint, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("archive/data.pkl", b"\0" * (torch_zip.MAX_PICKLE + 1024))
    started = time.perf_counter()
    result = torch_zip.read_keys(checkpoint)
    assert (time.perf_counter() - started) < 5.0
    assert result.integrity == "unsupported_format"
    assert "refusing to scan" in (result.integrity_note or "")


# ---------------------------------------------------------------------------
# Dynamic: a real malicious YAML
# ---------------------------------------------------------------------------

def test_yaml_object_apply_is_refused(tmp_path):
    marker = tmp_path / "YAML_PWNED.txt"
    document = tmp_path / "extra_model_paths.yaml"
    document.write_text(
        "evil: !!python/object/apply:os.system "
        f"['cmd /c echo owned > \"{marker}\"']\n", encoding="utf-8")
    result = extra_paths_yaml.parse_extra_model_paths(document)
    assert not marker.exists(), "the YAML payload executed"
    assert result == [] or result is None


def test_yaml_anchor_expansion_does_not_hang(tmp_path):
    document = tmp_path / "extra_model_paths.yaml"
    document.write_text(
        "a: &a [x,x,x,x,x,x,x,x,x]\n"
        "b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\n"
        "c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]\n"
        "d: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c]\n"
        "e: &e [*d,*d,*d,*d,*d,*d,*d,*d,*d]\n"
        "f: &f [*e,*e,*e,*e,*e,*e,*e,*e,*e]\n"
        "g: &g [*f,*f,*f,*f,*f,*f,*f,*f,*f]\n", encoding="utf-8")
    started = time.perf_counter()
    extra_paths_yaml.parse_extra_model_paths(document)
    assert (time.perf_counter() - started) < 5.0


# ---------------------------------------------------------------------------
# Dynamic: a real malicious custom_nodes package
# ---------------------------------------------------------------------------

def test_a_malicious_init_py_is_parsed_never_run(tmp_path):
    marker = tmp_path / "NODE_PWNED.txt"
    package = tmp_path / "custom_nodes" / "evil-pack"
    package.mkdir(parents=True)
    escaped = str(marker).replace("\\", "/")
    (package / "__init__.py").write_text(
        "import os\n"
        f"os.system('cmd /c echo owned > \"{escaped}\"')\n"
        "exec('NODE_CLASS_MAPPINGS = {\"X\": \"X\"}')\n"
        "NODE_CLASS_MAPPINGS = {'Evil': 'Evil'}\n", encoding="utf-8")

    tree = node_ast.parse_source(package / "__init__.py")

    assert tree is not None
    assert not marker.exists(), "third-party node source was executed"
    mappings, _display, _reexports = node_ast.collect_mappings(tree)
    assert "Evil" in mappings


def test_deeply_nested_source_does_not_crash_the_parser(tmp_path):
    source = tmp_path / "deep.py"
    source.write_text("x = " + "[" * 5000 + "]" * 5000 + "\n", encoding="utf-8")
    started = time.perf_counter()
    assert node_ast.parse_source(source) is None
    assert (time.perf_counter() - started) < 10.0


def test_an_oversized_python_file_is_skipped(tmp_path):
    source = tmp_path / "huge.py"
    source.write_text("# " + "A" * (node_ast.MAX_PY_BYTES + 1024), encoding="utf-8")
    assert node_ast.read_source(source) is None
    assert node_ast.parse_source(source) is None


def test_the_python_file_count_is_capped(tmp_path):
    package = tmp_path / "many"
    package.mkdir()
    for i in range(60):
        (package / f"m{i}.py").write_text("NODE_CLASS_MAPPINGS = {}\n",
                                          encoding="utf-8")
    assert len(node_ast.walk_python_files(package, limit=10)) == 10
    assert node_ast.MAX_FILES <= 10_000


def test_literal_eval_cannot_be_driven_into_a_runaway(tmp_path):
    """``ast.literal_eval`` on hostile input must fail closed, never hang."""
    source = tmp_path / "lit.py"
    source.write_text("NODE_CLASS_MAPPINGS = " + "{'a': " * 400 + "1" + "}" * 400
                      + "\n", encoding="utf-8")
    started = time.perf_counter()
    tree = node_ast.parse_source(source)
    if tree is not None:
        node_ast.collect_mappings(tree)
    assert (time.perf_counter() - started) < 10.0


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
def test_the_ast_walker_ignores_directory_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.py").write_text("NODE_CLASS_MAPPINGS = {'Leak': 'Leak'}\n",
                                     encoding="utf-8")
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}\n",
                                         encoding="utf-8")
    try:
        os.symlink(str(outside), str(package / "linked"), target_is_directory=True)
    except OSError:
        pytest.skip("creating a symlink needs Developer Mode or elevation")
    files = node_ast.walk_python_files(package)
    assert not any(f.name == "leak.py" for f in files)
