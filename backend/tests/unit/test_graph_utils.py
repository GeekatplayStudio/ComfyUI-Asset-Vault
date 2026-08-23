"""B1 — link-valued prompt inputs must resolve, and must never reach SQLite raw.

The original crash was ``sqlite3.ProgrammingError: Error binding parameter 8:
type 'list' is not supported``: ComfyUI's API-format ``prompt`` stores
``CLIPTextEncode.inputs.text`` as a node link (``['88:97', 0]``) far more often
than as a literal, and the scanner bound the list straight into the statement.
Because the commit was at the very end, every earlier insert rolled back too.

Two independent guarantees are asserted here, because either alone is fragile:

1. ``graph_utils`` resolves links to scalars (the parser is correct), and
2. ``db.bind`` coerces *anything* to a bindable scalar (the storage layer cannot
   be crashed even by a parser that regresses).
"""

from __future__ import annotations

import json
import random
import sqlite3

import pytest

from app.core import db as dbmod
from app.parsers import graph_utils as G

# ---------------------------------------------------------------------------
# Graph builders
# ---------------------------------------------------------------------------

POS = "a photograph of a lighthouse, golden hour, 85mm"
NEG = "blurry, watermark, text, low quality"


def link_graph(pos: str = POS, neg: str = NEG, *, prefix: str = "") -> dict:
    """The exact shape that crashed B1: sampler -> link -> CLIPTextEncode."""
    p = prefix
    return {
        f"{p}1": {"class_type": "CheckpointLoaderSimple",
                  "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
        f"{p}2": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": pos, "clip": [f"{p}1", 1]}},
        f"{p}3": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": neg, "clip": [f"{p}1", 1]}},
        f"{p}4": {"class_type": "EmptyLatentImage",
                  "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        f"{p}5": {"class_type": "KSampler",
                  "inputs": {"model": [f"{p}1", 0],
                             "positive": [f"{p}2", 0],
                             "negative": [f"{p}3", 0],
                             "latent_image": [f"{p}4", 0],
                             "seed": 123456789, "steps": 28, "cfg": 6.5,
                             "sampler_name": "dpmpp_2m", "scheduler": "karras",
                             "denoise": 1.0}},
        f"{p}6": {"class_type": "VAEDecode",
                  "inputs": {"samples": [f"{p}5", 0], "vae": [f"{p}1", 2]}},
    }


def chained_link_graph(depth: int = 6) -> dict:
    """``text`` itself is a link, through a chain of string-passthrough nodes.

    This is the case the 64 KB-window heuristic never saw: the literal is
    ``depth`` hops away from the encoder.
    """
    g: dict = {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": "model.safetensors"}},
        "100": {"class_type": "PrimitiveString", "inputs": {"value": POS}},
    }
    prev = "100"
    for i in range(depth):
        nid = f"1{i:02d}0"
        g[nid] = {"class_type": "StringConcatenate",
                  "inputs": {"string_a": [prev, 0], "string_b": ""}}
        prev = nid
    g["2"] = {"class_type": "CLIPTextEncode",
              "inputs": {"text": [prev, 0], "clip": ["1", 1]}}
    g["3"] = {"class_type": "CLIPTextEncode",
              "inputs": {"text": NEG, "clip": ["1", 1]}}
    g["5"] = {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
                         "seed": 7, "steps": 20, "cfg": 7.0,
                         "sampler_name": "euler", "scheduler": "normal"}}
    return g


def corpus(n: int = 200) -> list[tuple[str, dict]]:
    """``n`` graphs spanning every link shape seen in the real library."""
    rng = random.Random(1701)
    out: list[tuple[str, dict]] = []
    for i in range(n):
        kind = i % 8
        if kind == 0:
            out.append((f"plain-{i}", link_graph(f"{POS} #{i}")))
        elif kind == 1:
            # Subgraph-style compound node ids ("45:2") - real, and a parser trap.
            out.append((f"subgraph-{i}", link_graph(f"{POS} #{i}", prefix="88:")))
        elif kind == 2:
            out.append((f"chained-{i}", chained_link_graph(rng.randint(1, 9))))
        elif kind == 3:
            # Positive and negative consume the SAME conditioning output.
            g = link_graph(f"{POS} #{i}")
            g["5"]["inputs"]["negative"] = ["2", 0]
            out.append((f"shared-cond-{i}", g))
        elif kind == 4:
            # Encoder text is a link to a node that does not exist.
            g = link_graph(f"{POS} #{i}")
            g["2"]["inputs"]["text"] = ["does-not-exist", 0]
            out.append((f"dangling-{i}", g))
        elif kind == 5:
            # A cycle: resolution must terminate, not recurse forever.
            g = link_graph(f"{POS} #{i}")
            g["2"]["inputs"]["text"] = ["3", 0]
            g["3"]["inputs"]["text"] = ["2", 0]
            out.append((f"cyclic-{i}", g))
        elif kind == 6:
            # Numeric widgets arriving as links, not literals.
            g = link_graph(f"{POS} #{i}")
            g["900"] = {"class_type": "PrimitiveInt", "inputs": {"value": 34}}
            g["901"] = {"class_type": "PrimitiveFloat", "inputs": {"value": 4.25}}
            g["5"]["inputs"]["steps"] = ["900", 0]
            g["5"]["inputs"]["cfg"] = ["901", 0]
            g["5"]["inputs"]["seed"] = ["900", 0]
            out.append((f"link-widgets-{i}", g))
        else:
            # Everything hostile at once.
            g = link_graph(f"{POS} #{i}")
            g["2"]["inputs"]["text"] = [["nested"], {"dict": 1}]
            g["3"]["inputs"]["text"] = None
            g["5"]["inputs"]["steps"] = {"unexpected": "object"}
            g["5"]["inputs"]["cfg"] = float("nan")
            out.append((f"hostile-{i}", g))
    return out


CORPUS = corpus(200)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def test_is_link_recognises_comfyui_link_shape():
    assert G.is_link(["88:97", 0])
    assert G.is_link(["4", 1])
    assert G.is_link([4, 0])
    assert not G.is_link("a literal prompt")
    assert not G.is_link([])
    assert not G.is_link(["only-one-element"])
    assert not G.is_link({"node": "4"})
    assert not G.is_link(None)


def test_link_valued_text_resolves_to_the_literal():
    g = link_graph()
    s = G.summarize_graph(prompt=g)
    assert s.positive_prompt == POS
    assert s.negative_prompt == NEG


def test_chained_link_resolves_through_passthrough_nodes():
    s = G.summarize_graph(prompt=chained_link_graph(6))
    assert s.positive_prompt == POS, "a 6-hop string chain must still resolve"


def test_shared_conditioning_yields_no_negative_not_a_duplicate():
    """The B1 smoking gun: positive == negative meant the resolver gave up."""
    g = link_graph()
    g["5"]["inputs"]["negative"] = ["2", 0]
    s = G.summarize_graph(prompt=g)
    assert s.positive_prompt == POS
    assert s.negative_prompt != s.positive_prompt
    assert not s.negative_prompt


def test_cyclic_graph_terminates():
    g = link_graph()
    g["2"]["inputs"]["text"] = ["3", 0]
    g["3"]["inputs"]["text"] = ["2", 0]
    s = G.summarize_graph(prompt=g)  # must return, not hang or recurse
    assert s.node_count == len(g)


def test_numeric_widgets_arriving_as_links_resolve_to_numbers():
    g = link_graph()
    g["900"] = {"class_type": "PrimitiveInt", "inputs": {"value": 34}}
    g["901"] = {"class_type": "PrimitiveFloat", "inputs": {"value": 4.25}}
    g["5"]["inputs"]["steps"] = ["900", 0]
    g["5"]["inputs"]["cfg"] = ["901", 0]
    s = G.summarize_graph(prompt=g)
    assert s.steps == 34
    assert s.cfg == pytest.approx(4.25)


def test_compound_subgraph_node_ids_resolve():
    s = G.summarize_graph(prompt=link_graph(prefix="88:"))
    assert s.positive_prompt == POS
    assert s.negative_prompt == NEG


@pytest.mark.parametrize("name,graph", CORPUS, ids=[n for n, _ in CORPUS])
def test_every_summary_field_is_sqlite_bindable(name, graph):
    """No field of a GraphSummary may ever be a list, dict, or object."""
    s = G.summarize_graph(prompt=graph)
    scalar = (str, int, float, bytes, type(None))
    for field_name in ("positive_prompt", "negative_prompt", "seed", "steps", "cfg",
                       "sampler", "scheduler", "denoise", "width", "height",
                       "graph_hash", "primary_model"):
        v = getattr(s, field_name)
        assert isinstance(v, scalar), (
            f"{name}.{field_name} is {type(v).__name__} — this is exactly B1"
        )


def test_no_graph_in_the_corpus_reports_positive_equal_to_negative():
    """Gate: ``pos == neg`` count across the corpus is 0."""
    offenders = []
    for name, graph in CORPUS:
        s = G.summarize_graph(prompt=graph)
        if s.positive_prompt and s.positive_prompt == s.negative_prompt:
            offenders.append(name)
    assert offenders == [], f"{len(offenders)} graphs reported pos == neg: {offenders[:5]}"


def test_summarize_never_raises_on_hostile_input():
    for bad in (None, [], {}, "", 0, "not a graph", {"1": None}, {"1": {"inputs": None}},
                {"1": {"class_type": 5, "inputs": {"text": [None, None]}}},
                {"1": {"class_type": "X", "inputs": {"text": ["1", 0]}}}):
        G.summarize_graph(prompt=bad)


# ---------------------------------------------------------------------------
# Storage layer — the second, independent guarantee
# ---------------------------------------------------------------------------

UNBINDABLE = [
    ["88:97", 0],
    {"a": 1},
    ({"t"}, ),
    object(),
    float("nan"),
    float("inf"),
    b"\xff\xfe raw",
    range(3),
]


@pytest.mark.parametrize("value", UNBINDABLE, ids=lambda v: type(v).__name__)
def test_bind_guard_makes_the_b1_crash_class_impossible(value):
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (v)")
        conn.execute("INSERT INTO t VALUES (?)", (dbmod.bind(value),))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        conn.close()


def test_bind_guard_preserves_useful_scalars():
    assert dbmod.bind("text") == "text"
    assert dbmod.bind(42, kind="int") == 42
    assert dbmod.bind(None) is None
    assert dbmod.bind(True, kind="int") in (1, True)


def test_a_full_corpus_insert_commits_every_row():
    """The regression in full: 200 graphs, one statement each, all persisted."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE outputs (id INTEGER PRIMARY KEY, name TEXT, "
                     "positive_prompt TEXT, negative_prompt TEXT, seed TEXT, "
                     "steps INTEGER, cfg REAL, sampler TEXT, graph_hash TEXT, "
                     "provenance_json TEXT)")
        for name, graph in CORPUS:
            s = G.summarize_graph(prompt=graph)
            conn.execute(
                "INSERT INTO outputs (name, positive_prompt, negative_prompt, seed, "
                "steps, cfg, sampler, graph_hash, provenance_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (dbmod.bind(name), dbmod.bind(s.positive_prompt),
                 dbmod.bind(s.negative_prompt), dbmod.bind(s.seed),
                 dbmod.bind(s.steps, kind="int"), dbmod.bind(s.cfg, kind="real"),
                 dbmod.bind(s.sampler), dbmod.bind(s.graph_hash),
                 dbmod.bind(json.dumps(s.provenance, default=str))))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM outputs").fetchone()[0] == len(CORPUS)
        # and the useful data actually landed, not just empty rows
        resolved = conn.execute(
            "SELECT COUNT(*) FROM outputs WHERE positive_prompt IS NOT NULL "
            "AND positive_prompt <> ''").fetchone()[0]
        assert resolved >= len(CORPUS) * 0.6, (
            f"only {resolved}/{len(CORPUS)} graphs yielded a prompt")
        assert conn.execute(
            "SELECT COUNT(*) FROM outputs WHERE positive_prompt IS NOT NULL "
            "AND positive_prompt <> '' AND positive_prompt = negative_prompt"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_provenance_is_recorded_for_resolved_prompts():
    s = G.summarize_graph(prompt=link_graph())
    assert s.provenance, "resolution must record where each value came from"
    assert json.dumps(s.provenance, default=str)
