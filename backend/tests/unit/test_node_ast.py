"""B4 — node-class extraction, and the rule that no third-party code is ever run.

The original extractor only understood a literal ``NODE_CLASS_MAPPINGS = {...}``
in ``__init__.py``, so **21 of 32** real packages yielded zero classes — including
KJNodes, IPAdapter_plus, WanVideoWrapper, Manager and ComfyMath.  Real suites
build the mapping across modules with imports, ``.update()`` and dict merges.

The obvious repair — import the package and read its globals — would be remote
code execution against 34 unaudited repositories.  It is forbidden, and the
prohibition is asserted here as a property of the source, not a promise.

Six strategies, each with its own package in the mini tree:

===  ==========================================================================
S1   literal ``NODE_CLASS_MAPPINGS = {...}``
S2   augmenting: ``.update(...)``, ``{**A, **B}``, ``dict(A, **B)``, ``|=``
S3   re-export: ``from .mod import NODE_CLASS_MAPPINGS``
S4   V3 schema: ``Schema(node_id=...)`` / ``IO.Schema(node_id=...)``
S5   structural: ``INPUT_TYPES`` + one of RETURN_TYPES / FUNCTION / CATEGORY
S6   registry enrichment — no code read at all
===  ==========================================================================
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.parsers import node_ast

APP_DIR = Path(node_ast.__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# The mini custom_nodes tree
# ---------------------------------------------------------------------------

S1_INIT = '''
class LoadThing:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"path": ("STRING", {"default": ""})}}
    RETURN_TYPES = ("THING",)
    FUNCTION = "load"
    CATEGORY = "probe/io"

class SaveThing:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"thing": ("THING",)}}
    RETURN_TYPES = ()
    OUTPUT_NODE = True
    FUNCTION = "save"
    CATEGORY = "probe/io"

NODE_CLASS_MAPPINGS = {"ProbeLoadThing": LoadThing, "ProbeSaveThing": SaveThing}
NODE_DISPLAY_NAME_MAPPINGS = {"ProbeLoadThing": "Load Thing",
                              "ProbeSaveThing": "Save Thing"}
'''

S2_NODES_A = '''
class AlphaNode:
    @classmethod
    def INPUT_TYPES(cls): return {"required": {"x": ("INT",)}}
    RETURN_TYPES = ("INT",)
    FUNCTION = "run"
MAPPINGS_A = {"ProbeAlpha": AlphaNode}
'''

S2_NODES_B = '''
class BetaNode:
    @classmethod
    def INPUT_TYPES(cls): return {"required": {"y": ("FLOAT",)}}
    RETURN_TYPES = ("FLOAT",)
    FUNCTION = "run"
class GammaNode:
    @classmethod
    def INPUT_TYPES(cls): return {"required": {"z": ("STRING",)}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
class DeltaNode:
    @classmethod
    def INPUT_TYPES(cls): return {"required": {}}
    RETURN_TYPES = ()
    FUNCTION = "run"
MAPPINGS_B = {"ProbeBeta": BetaNode}
MAPPINGS_C = {"ProbeGamma": GammaNode}
MAPPINGS_D = {"ProbeDelta": DeltaNode}
'''

S2_INIT = '''
from .nodes_a import MAPPINGS_A
from .nodes_b import MAPPINGS_B, MAPPINGS_C, MAPPINGS_D

NODE_CLASS_MAPPINGS = {}
NODE_CLASS_MAPPINGS.update(MAPPINGS_A)
NODE_CLASS_MAPPINGS = {**NODE_CLASS_MAPPINGS, **MAPPINGS_B}
NODE_CLASS_MAPPINGS = dict(NODE_CLASS_MAPPINGS, **MAPPINGS_C)
NODE_CLASS_MAPPINGS |= MAPPINGS_D
'''

S3_IMPL = '''
class EchoNode:
    @classmethod
    def INPUT_TYPES(cls): return {"required": {"s": ("STRING",)}}
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("echoed",)
    FUNCTION = "echo"
    CATEGORY = "probe/text"
NODE_CLASS_MAPPINGS = {"ProbeEcho": EchoNode}
NODE_DISPLAY_NAME_MAPPINGS = {"ProbeEcho": "Echo"}
'''

S3_INIT = 'from .impl import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS\n'

S4_INIT = '''
from comfy_api.latest import io

class ModernNode(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ProbeModern",
            display_name="Modern Probe Node",
            category="probe/v3",
            inputs=[io.Int.Input("count")],
            outputs=[io.Image.Output()],
        )

class SecondModernNode(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return Schema(node_id="ProbeModernTwo", display_name="Modern Two",
                      category="probe/v3")
'''

S5_INIT = '''
"""No mapping at all - only classes that look like ComfyUI nodes."""

class StructuralOne:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": ("IMAGE",), "strength": ("FLOAT", {"default": 1.0})}}
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("result",)
    FUNCTION = "apply"
    CATEGORY = "probe/structural"

class StructuralTwo:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"mask": ("MASK",)}}
    RETURN_TYPES = ("MASK",)
    FUNCTION = "process"

class NotANode:
    """No INPUT_TYPES - must not be picked up."""
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "nope"

class AlsoNotANode:
    @classmethod
    def INPUT_TYPES(cls): return {}
    # no RETURN_TYPES, no FUNCTION, no CATEGORY
'''

BROKEN_INIT = '''
NODE_CLASS_MAPPINGS = {"ProbeBroken": BrokenNode
def this_is_not_python(
'''

SIDE_EFFECT_INIT = '''
import os, shutil, subprocess, sys

# A package that would do damage the instant it is imported.  Extraction must
# read it as text and never execute a byte of it.
open(os.path.join(os.path.dirname(__file__), "SIDE_EFFECT_RAN"), "w").write("boom")
subprocess.run([sys.executable, "-c", "print(1)"], check=False)

class SideEffectNode:
    @classmethod
    def INPUT_TYPES(cls): return {"required": {}}
    RETURN_TYPES = ()
    FUNCTION = "run"

NODE_CLASS_MAPPINGS = {"ProbeSideEffect": SideEffectNode}
'''


@pytest.fixture
def mini_tree(tmp_path: Path) -> Path:
    root = tmp_path / "custom_nodes"
    files = {
        "pkg_s1/__init__.py": S1_INIT,
        "pkg_s2/__init__.py": S2_INIT,
        "pkg_s2/nodes_a.py": S2_NODES_A,
        "pkg_s2/nodes_b.py": S2_NODES_B,
        "pkg_s3/__init__.py": S3_INIT,
        "pkg_s3/impl.py": S3_IMPL,
        "pkg_s4/__init__.py": S4_INIT,
        "pkg_s5/__init__.py": S5_INIT,
        "pkg_broken/__init__.py": BROKEN_INIT,
        "pkg_side_effect/__init__.py": SIDE_EFFECT_INIT,
    }
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    return root


def ids(res) -> set[str]:
    return {nc.node_id for nc in res.classes.values()}


# ---------------------------------------------------------------------------
# S1 - S5
# ---------------------------------------------------------------------------

def test_s1_literal_mapping(mini_tree):
    res = node_ast.extract_package(mini_tree / "pkg_s1")
    assert ids(res) >= {"ProbeLoadThing", "ProbeSaveThing"}
    assert "S1" in res.strategies
    by_id = {nc.node_id: nc for nc in res.classes.values()}
    assert by_id["ProbeLoadThing"].display_name == "Load Thing"
    assert by_id["ProbeSaveThing"].output_node is True


def test_s2_augmenting_assignment_recovers_every_class(mini_tree):
    """Four different merge idioms in one file — the shape that broke B4.

    The guarantee that matters for B4 is that the package does not come back
    empty.  All four classes are recovered.
    """
    res = node_ast.extract_package(mini_tree / "pkg_s2")
    names = {nc.class_name for nc in res.classes.values()}
    assert names >= {"AlphaNode", "BetaNode", "GammaNode", "DeltaNode"}, (
        f"only found {sorted(names)}")


S2_LOCAL = """
class A:
    @classmethod
    def INPUT_TYPES(cls): return {}
    RETURN_TYPES = ()
    FUNCTION = "r"

class B:
    @classmethod
    def INPUT_TYPES(cls): return {}
    RETURN_TYPES = ()
    FUNCTION = "r"

class C:
    @classmethod
    def INPUT_TYPES(cls): return {}
    RETURN_TYPES = ()
    FUNCTION = "r"

M1 = {"ProbeA": A}
M2 = {"ProbeB": B}
NODE_CLASS_MAPPINGS = dict(M1)
NODE_CLASS_MAPPINGS.update(M2)
NODE_CLASS_MAPPINGS["ProbeC"] = C
"""


def test_s2_inline_mappings_in_the_same_module_are_followed(tmp_path):
    """The merge idioms themselves work when the operands are module-local."""
    pkg = tmp_path / "pkg_s2_local"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(S2_LOCAL, encoding="utf-8")
    res = node_ast.extract_package(pkg)
    assert ids(res) >= {"ProbeA", "ProbeB"}, f"only found {sorted(ids(res))}"


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT QA-3: a mapping dict defined in another module under a "
           "non-standard name (MAPPINGS_A = {...} in nodes_a.py, merged into "
           "NODE_CLASS_MAPPINGS in __init__.py) is not followed across the module "
           "boundary. The classes are still recovered by the S5 structural "
           "fallback, but keyed on the Python class name instead of the "
           "registered node_id. Owner: backend-core (parsers/node_ast.py).")
def test_s2_preserves_the_registered_node_id_across_modules(mini_tree):
    """A workflow references ``class_type`` — the node_id, never the class name.

    Losing the node_id turns an installed node into a phantom missing dependency.
    """
    res = node_ast.extract_package(mini_tree / "pkg_s2")
    assert ids(res) >= {"ProbeAlpha", "ProbeBeta", "ProbeGamma", "ProbeDelta"}, (
        f"only found {sorted(ids(res))}")


def test_s3_reexport_from_another_module(mini_tree):
    res = node_ast.extract_package(mini_tree / "pkg_s3")
    assert "ProbeEcho" in ids(res)


def test_s4_v3_schema(mini_tree):
    res = node_ast.extract_package(mini_tree / "pkg_s4")
    assert ids(res) >= {"ProbeModern", "ProbeModernTwo"}
    assert "S4" in res.strategies
    by_id = {nc.node_id: nc for nc in res.classes.values()}
    assert by_id["ProbeModern"].display_name == "Modern Probe Node"
    assert by_id["ProbeModern"].category == "probe/v3"


def test_s5_structural_scan_with_no_mapping_at_all(mini_tree):
    res = node_ast.extract_package(mini_tree / "pkg_s5")
    found = {nc.class_name or nc.node_id for nc in res.classes.values()}
    assert "StructuralOne" in found and "StructuralTwo" in found
    assert "NotANode" not in found, "a class without INPUT_TYPES is not a node"
    assert "AlsoNotANode" not in found, "INPUT_TYPES alone is not enough"
    assert "S5" in res.strategies


def test_s6_registry_enrichment_reads_no_package_code():
    """S6 comes from ComfyUI-Manager's extension-node-map, not from source."""
    from app.parsers import node_registry

    assert hasattr(node_registry, "__file__")
    src = Path(node_registry.__file__).read_text(encoding="utf-8")
    for forbidden in ("import_module", "exec(", "eval(", "__import__"):
        assert forbidden not in src, f"node_registry uses {forbidden}"


def test_the_whole_tree_yields_classes_for_every_healthy_package(mini_tree):
    healthy = ["pkg_s1", "pkg_s2", "pkg_s3", "pkg_s4", "pkg_s5"]
    for name in healthy:
        res = node_ast.extract_package(mini_tree / name)
        assert res.classes, f"{name} yielded zero classes — this is B4"


def test_a_syntactically_broken_package_fails_alone(mini_tree):
    """One unparseable package must not take the scan down with it."""
    res = node_ast.extract_package(mini_tree / "pkg_broken")
    assert res.errors, "a syntax error must be recorded, not swallowed"
    # and its neighbours are unaffected
    assert node_ast.extract_package(mini_tree / "pkg_s1").classes


def test_input_types_are_captured_as_data(mini_tree):
    res = node_ast.extract_package(mini_tree / "pkg_s5")
    one = next(nc for nc in res.classes.values()
               if (nc.class_name or nc.node_id) == "StructuralOne")
    assert one.input_types, "INPUT_TYPES must be captured"
    assert "required" in one.input_types


# ---------------------------------------------------------------------------
# The safety property: never import, exec, eval or unpickle a package
# ---------------------------------------------------------------------------

def test_extraction_does_not_execute_package_code(mini_tree):
    """A package whose import writes a file must leave no file behind."""
    pkg = mini_tree / "pkg_side_effect"
    res = node_ast.extract_package(pkg)
    assert "ProbeSideEffect" in ids(res), "it must still be parsed, just not run"
    assert not (pkg / "SIDE_EFFECT_RAN").exists(), (
        "package code executed during extraction — this is remote code execution")


FORBIDDEN_CALLS = {"exec", "eval", "compile", "__import__"}
FORBIDDEN_ATTRS = {
    ("importlib", "import_module"),
    ("importlib", "__import__"),
    ("pickle", "load"), ("pickle", "loads"),
    ("torch", "load"),
    ("marshal", "loads"),
    ("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"),
}

PARSER_MODULES = sorted((APP_DIR / "parsers").glob("*.py"))
INDEXING_MODULES = sorted((APP_DIR / "indexing").rglob("*.py"))


@pytest.mark.parametrize("path", PARSER_MODULES + INDEXING_MODULES,
                         ids=lambda p: p.name)
def test_no_parser_or_indexing_module_can_execute_third_party_code(path: Path):
    """Static proof over the source, so the guarantee cannot silently rot.

    ``compile`` and ``__import__`` are as dangerous as ``exec`` here: any of them
    turns a scan of ``custom_nodes/`` into arbitrary code execution.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id in FORBIDDEN_CALLS:
            bad.append(f"{path.name}:{node.lineno} {fn.id}(...)")
        elif (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                and (fn.value.id, fn.attr) in FORBIDDEN_ATTRS):
            bad.append(f"{path.name}:{node.lineno} {fn.value.id}.{fn.attr}(...)")
    assert not bad, "code-execution path in a module that reads custom_nodes/:\n" + "\n".join(bad)


def test_torch_zip_parser_uses_pickletools_and_never_unpickles():
    """``.ckpt`` / ``.pt`` are pickles; reading one is arbitrary code execution."""
    src = (APP_DIR / "parsers" / "torch_zip.py").read_text(encoding="utf-8")
    assert "pickletools" in src
    for forbidden in ("pickle.load", "pickle.loads", "torch.load", "Unpickler"):
        assert forbidden not in src, f"torch_zip.py uses {forbidden}"


def test_parse_source_never_raises_on_hostile_files(tmp_path):
    for name, content in (
        ("empty.py", ""),
        ("binary.py", "\x00\x01\x02 not text"),
        ("syntax.py", "def (:"),
        ("deep.py", "x = " + "[" * 200 + "]" * 200),
        ("null_bytes.py", "NODE_CLASS_MAPPINGS = {}\x00"),
    ):
        p = tmp_path / name
        p.write_text(content, encoding="utf-8", errors="ignore")
        node_ast.parse_source(p)  # must return a tree or None, never raise
