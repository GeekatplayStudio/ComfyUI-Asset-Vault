"""The two data files that ship with the System Advisor.

``app/advisor/data/model_catalog.json`` is what the advisor answers from on
first launch and whenever outbound lookups are off; ``sources.json`` is the list
of where a refresh looks.  Both are plain JSON that a release carries verbatim,
so this file is the review a hand edit or a regenerated snapshot has to pass:
well-formed, https-only, nothing flagged adult, every pinned repository a real
``owner/name``, and no row that could put a host on the allowlist.

These read the shipped files directly - never ``data/advisor/``, which belongs
to whoever ran the app on this machine.
"""

from __future__ import annotations

import json
from collections import Counter
from urllib.parse import urlsplit

import pytest

from app.advisor import catalog, fit, security, sources, updater
from app.enable import hosts


@pytest.fixture(scope="module")
def seed() -> dict:
    with open(catalog.SEED_CATALOG, encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def source_list() -> dict:
    with open(catalog.SEED_SOURCES, encoding="utf-8") as handle:
        return json.load(handle)


# -------------------------------------------------------------------- catalog

def test_the_seed_files_live_beside_the_package_not_in_the_user_data_dir():
    for path in (catalog.SEED_CATALOG, catalog.SEED_SOURCES):
        assert path.is_file()
        assert path.parent.name == "data"
        assert path.parent.parent.name == "advisor"
    assert catalog.USER_DIR not in catalog.SEED_CATALOG.parents


def test_the_seed_catalog_has_the_documented_header(seed):
    assert seed["schema"] == catalog.SCHEMA == 1
    assert isinstance(seed["generated_at"], int) and seed["generated_at"] > 0
    assert isinstance(seed["entries"], list)
    assert isinstance(seed["sources"], dict)
    assert isinstance(seed["leads"], list)
    assert len(seed["entries"]) >= 500, "a seed this thin would not be worth shipping"


def test_every_seed_entry_is_a_valid_catalog_entry(seed):
    bad = [e.get("id") if isinstance(e, dict) else repr(e)[:40]
           for e in seed["entries"] if not catalog._valid_entry(e)]
    assert not bad, bad[:10]


def test_seed_ids_are_unique_and_carry_a_known_source_prefix(seed):
    counts = Counter(e["id"] for e in seed["entries"])
    assert not [k for k, n in counts.items() if n > 1]
    prefix_by_source = {"huggingface": "hf:", "civitai": "civitai:",
                        "comfy_manager": "comfy:", "openmodeldb": "omdb:",
                        "ollama": "ollama:"}
    for entry in seed["entries"]:
        assert entry["source"] in prefix_by_source, entry["id"]
        assert entry["id"].startswith(prefix_by_source[entry["source"]]), entry["id"]
        assert len(entry["id"]) <= 240, "must fit the ?id= parameter"


def test_every_hub_entry_id_is_a_valid_repository_id(seed):
    for entry in seed["entries"]:
        if entry["source"] == "huggingface":
            assert sources.valid_repo_id(entry["id"][3:]), entry["id"]


def test_every_link_in_the_seed_is_https(seed):
    def links():
        for entry in seed["entries"]:
            yield entry["id"], entry.get("url")
            for row in entry["variants"] + (entry.get("companions") or []):
                yield entry["id"], row.get("download_url")
        for lead in seed["leads"]:
            yield lead.get("name"), lead.get("url")

    seen = 0
    for owner, url in links():
        if url is None:
            continue
        seen += 1
        parts = urlsplit(url)
        assert parts.scheme == "https", (owner, url)
        assert parts.hostname, (owner, url)
        assert not parts.username and "@" not in parts.netloc, (owner, url)
        assert not any(ord(ch) < 0x20 for ch in url), (owner, url)
    assert seen > len(seed["entries"])
    assert all(e.get("url") for e in seed["entries"]), "every entry links to its page"


def test_no_seed_entry_is_flagged_adult(seed):
    assert not [e["id"] for e in seed["entries"] if e.get("nsfw")]
    assert not [e["id"] for e in seed["entries"]
                if "not-for-all-audiences" in [str(t).lower() for t in e.get("tags") or []]]


def test_every_seed_entry_and_lead_passes_the_catalog_suitability_filter(seed):
    """``catalog.load`` drops what ``suitable`` refuses, so a seed row that fails
    it would ship and never be shown."""
    assert not [e["id"] for e in seed["entries"] if not catalog.suitable(e)]
    assert not [lead.get("name") for lead in seed["leads"]
                if not catalog.suitable_lead(lead)]


@pytest.mark.parametrize("entry,expected", [
    ({"id": "hf:demo-labs/Demo-Image", "name": "Demo Image", "tags": ["comfyui"]}, True),
    ({"id": "hf:demo-labs/Demo-Image", "name": "Demo Image", "nsfw": True}, False),
    ({"id": "hf:someone/Demo-NSFW-LoRA", "name": "Demo"}, False),
    ({"id": "hf:someone/demo", "name": "Demo 7B Uncensored GGUF"}, False),
    ({"id": "hf:someone/demo", "name": "Demo", "tags": ["abliterated"]}, False),
    ({"id": "civitai:1", "name": "Classic Glass Bottles"}, True),
    ({}, True),
])
def test_the_suitability_filter(entry, expected):
    assert catalog.suitable(entry) is expected


def test_variant_ids_are_unique_within_every_seed_entry(seed):
    clashes = [e["id"] for e in seed["entries"]
               if len({v.get("id") for v in e["variants"]}) != len(e["variants"])]
    assert not clashes, clashes


def test_every_seed_variant_has_an_id_a_size_and_a_format(seed):
    for entry in seed["entries"]:
        ids = [v.get("id") for v in entry["variants"]]
        assert all(isinstance(i, str) and i for i in ids), entry["id"]
        assert len(ids) <= sources.MAX_VARIANTS
        for variant in entry["variants"]:
            assert isinstance(variant.get("bytes"), int) and variant["bytes"] > 0, \
                (entry["id"], variant.get("id"))
            assert variant.get("format"), (entry["id"], variant.get("id"))
        for companion in entry.get("companions") or []:
            assert companion.get("role"), entry["id"]
            assert isinstance(companion.get("bytes"), int) and companion["bytes"] > 0


def test_the_seed_covers_the_model_types_the_interface_leads_with(seed):
    kinds = Counter(e["kind"] for e in seed["entries"])
    for kind in ("image", "video", "llm", "lora", "audio", "upscale"):
        assert kinds[kind] >= 1, f"no {kind} entry in the seed"
    assert set(kinds) <= set(fit.KINDS)


def test_the_seed_holds_no_secret_and_nothing_about_a_machine(seed):
    text = catalog.SEED_CATALOG.read_text(encoding="utf-8")
    for needle in ("hf_", "Bearer ", "api_key", "Authorization", "C:\\\\Users",
                   "/Users/", "/home/"):
        assert needle not in text, needle
    assert seed.get("generator", "").startswith("Geekatplay ComfyUI Asset Vault")


def test_every_seed_entry_can_be_assessed_and_scored(seed):
    """The whole seed runs through the arithmetic without an exception."""
    budget = fit.make_budget({"accelerator": {"backend": "cuda",
                                              "vram_total_bytes": 12 * fit.GIB},
                              "memory": {"total_bytes": 32 * fit.GIB}})
    verdicts = Counter()
    for entry in seed["entries"]:
        assessment = fit.assess_entry(entry, budget)
        assert assessment["verdict"] in fit.VERDICT_ORDER
        assert assessment["inferred"] is True
        verdicts[assessment["verdict"]] += 1
        score = security.score_entry(entry)
        assert 0 <= score["score"] <= 99
    assert verdicts["unknown"] == 0, "every seed entry has a size to reason about"
    assert verdicts["optimal"] > 0 and verdicts["base"] > 0


def test_the_seed_catalog_is_what_load_serves_when_nothing_was_refreshed(
        seed, tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "USER_DIR", tmp_path / "advisor")
    monkeypatch.setattr(catalog, "USER_CATALOG", tmp_path / "advisor" / "catalog.json")
    monkeypatch.setattr(catalog, "USER_STATE", tmp_path / "advisor" / "state.json")
    catalog.invalidate()
    try:
        loaded = catalog.load()
        assert loaded["origin"] == "seed"
        assert loaded["generated_at"] == seed["generated_at"]
        assert len(loaded["entries"]) == len(seed["entries"])
        assert not (tmp_path / "advisor").exists(), "reading never creates the folder"
    finally:
        catalog.invalidate()


# ---------------------------------------------------------------- source list

def test_the_source_list_passes_its_own_shape_check(source_list):
    assert catalog.valid_sources_doc(source_list)
    assert isinstance(source_list["revision"], int) and source_list["revision"] >= 1


@pytest.mark.parametrize("doc", [
    None, [], "x", {}, {"revision": "1", "sources": []}, {"revision": 1},
    {"revision": 1, "sources": {}}, {"revision": 1, "sources": [], "pins": []},
    {"revision": 1, "sources": [], "discovery": "x"},
    {"revision": 1, "sources": [], "publishers": []},
])
def test_a_malformed_source_list_is_rejected(doc):
    assert catalog.valid_sources_doc(doc) is False


def test_every_source_row_is_one_the_updater_knows_with_an_https_address(source_list):
    ids = [row["id"] for row in source_list["sources"]]
    assert len(ids) == len(set(ids))
    assert set(ids) <= set(updater.SOURCE_IDS)
    assert {"huggingface", "civitai", "comfy_manager", "openmodeldb", "ollama",
            "github"} <= set(ids)
    for row in source_list["sources"]:
        parts = urlsplit(row["url"])
        assert parts.scheme == "https", row
        assert parts.hostname, row
        assert not parts.username and "@" not in parts.netloc, row
        assert row["name"] and row["provides"]
        assert set(row.get("kinds") or []) <= set(fit.KINDS), row["id"]


def test_every_pinned_repository_is_a_valid_repository_id(source_list):
    pins = source_list["pins"]["huggingface"]
    assert len(pins) >= 20
    repos = [pin["repo"] for pin in pins]
    assert len(repos) == len(set(repos)), "a repository is pinned once"
    for pin in pins:
        assert sources.valid_repo_id(pin["repo"]), pin
        assert pin.get("kind") is None or pin["kind"] in fit.KINDS, pin
        if "quality" in pin:
            assert 0 <= pin["quality"] <= 100, pin
    assert set(source_list["pins"]) == {"huggingface"}, \
        "only Hub repositories can be pinned"


def test_every_discovery_query_builds_a_url_on_the_catalog_allowlist(source_list):
    discovery = source_list["discovery"]
    assert set(discovery) == {"huggingface", "civitai", "github", "openmodeldb",
                              "follow_base_models"}
    urls = [sources.hf_list_url(q) for q in discovery["huggingface"]]
    urls += [sources.civitai_list_url(q) for q in discovery["civitai"]]
    urls += [sources.github_search_url(q) for q in discovery["github"]]
    for url in urls:
        assert hosts.check(url, kind=hosts.KIND_CATALOG).url == url
    for query in discovery["huggingface"]:
        assert query.get("kind") is None or query["kind"] in fit.KINDS, query
        # Each value survives the builder's own filter - none is silently dropped.
        built = sources.hf_list_url(query)
        for key in ("pipeline_tag", "filter", "author", "library", "search"):
            if query.get(key):
                assert f"{key}=" in built, (key, query)
    for query in discovery["civitai"]:
        assert query["types"] in sources.CIVITAI_TYPES, query
    assert isinstance(discovery["follow_base_models"].get("enabled"), bool)
    assert 0 < discovery["follow_base_models"]["limit"] <= 80
    assert 0 < discovery["openmodeldb"]["limit"] <= 400


def test_recognised_publishers_are_plain_owner_names(source_list):
    publishers = source_list["publishers"]
    assert set(publishers) == {"official", "trusted"}
    for names in publishers.values():
        assert len(names) == len(set(names))
        for name in names:
            assert sources.valid_repo_id(f"{name}/x"), name
    assert not set(publishers["official"]) & set(publishers["trusted"])


def test_the_source_list_cannot_name_a_host_or_carry_a_secret(source_list):
    """Nothing in the file is read as an endpoint: the only ``url`` keys are the
    display links on the source rows, and no discovery query carries one."""
    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                yield f"{path}.{key}", key, value
                yield from walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from walk(value, f"{path}[{index}]")

    for where, key, value in walk(source_list):
        if key in ("url", "host", "endpoint", "base_url"):
            assert where.startswith(".sources["), where
        assert key.lower() not in ("token", "api_key", "authorization", "cookie",
                                   "password", "headers"), where
        if isinstance(value, str) and "://" in value:
            assert where.startswith(".sources[") or where == ".about", where
