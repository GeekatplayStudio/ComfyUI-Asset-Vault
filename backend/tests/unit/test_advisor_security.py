"""The advisor's safety score: explainable, bounded, and never a clean bill of health.

Three things are pinned.  The **ordering** people rely on (a data-only format
beats a pickle, a flagged host scan sinks the score, the original publisher beats
an unknown one).  The **bounds**: 0 to 99, never 100, because the score is an
estimate from published facts and the ceiling says so.  And the **arithmetic**:
every point added or removed is one line in ``factors``, so the number can always
be read back from the payload.
"""

from __future__ import annotations

import itertools

import pytest

from app.advisor import security

NOW = 1_800_000_000.0  # a fixed "now" so age factors do not drift with the calendar
OLD = "2024-01-01T00:00:00.000Z"
FACTOR_KEYS = {"id", "label", "points", "detail", "tone"}
TONES = {"ok", "warn", "danger", "neutral"}


def entry(fmt: str = "safetensors", **overrides) -> dict:
    safety = {"scan": "unknown", "publisher_trust": "unknown", "code_files": 0,
              "custom_code": False, "hash_published": False}
    safety.update(overrides.pop("safety", {}))
    base = {"id": "hf:someone/model", "source": "huggingface", "publisher": "someone",
            "license": "apache-2.0", "downloads": 5_000, "created_at": OLD,
            "variants": [{"id": "v", "format": fmt, "bytes": 1}], "safety": safety}
    base.update(overrides)
    return base


def score(*args, **kwargs) -> int:
    return security.score_entry(entry(*args, **kwargs), now=NOW)["score"]


def unclamped(result: dict) -> int:
    return security.BASE_SCORE + sum(f["points"] for f in result["factors"])


# ------------------------------------------------------------------ ordering

@pytest.mark.parametrize("safe", ["safetensors", "gguf", "sft", "onnx"])
@pytest.mark.parametrize("pickle", ["ckpt", "pt", "pth", "bin", "pkl"])
def test_a_data_only_format_always_beats_a_pickle(safe, pickle):
    assert score(safe) > score(pickle)
    assert score(safe) - score(pickle) == 50, "+25 against -25"


def test_the_format_factor_names_what_it_found():
    def factor(result):
        return next(f for f in result["factors"] if f["id"] == "format")

    safe = factor(security.score_entry(entry("safetensors"), now=NOW))
    assert (safe["label"], safe["points"], safe["tone"]) == ("Data-only format", 25, "ok")
    pickle = factor(security.score_entry(entry("ckpt"), now=NOW))
    assert (pickle["label"], pickle["points"], pickle["tone"]) == \
        ("Pickle format", -25, "danger")
    assert "can run code" in pickle["detail"]
    mixed = factor(security.score_entry(entry(variants=[
        {"id": "a", "format": "safetensors"}, {"id": "b", "format": "ckpt"}]), now=NOW))
    assert (mixed["label"], mixed["points"], mixed["tone"]) == ("Mixed formats", -8, "warn")
    unknown = factor(security.score_entry(entry(variants=[]), now=NOW))
    assert (unknown["label"], unknown["points"]) == ("Format unknown", -5)


def test_scoring_one_variant_judges_that_file_not_the_whole_repository():
    mixed = entry(variants=[{"id": "a", "format": "safetensors"},
                            {"id": "b", "format": "ckpt"}])
    safe_file = security.score_entry(mixed, mixed["variants"][0], now=NOW)["score"]
    pickle_file = security.score_entry(mixed, mixed["variants"][1], now=NOW)["score"]
    whole = security.score_entry(mixed, now=NOW)["score"]
    assert pickle_file < whole < safe_file


@pytest.mark.parametrize("raw,normal", [
    ("SafeTensor", "safetensors"), (".safetensors", "safetensors"),
    ("PickleTensor", "ckpt"), ("Pickle Tensor", "ckpt"), ("diffusers", "safetensors"),
    (".GGUF", "gguf"), (None, ""), ("", ""),
])
def test_format_names_are_normalised(raw, normal):
    assert security.normalize_format(raw) == normal


def test_a_flagged_host_scan_drops_the_score_hard():
    clean = score(safety={"scan": "clean"})
    unknown = score(safety={"scan": "unknown"})
    pending = score(safety={"scan": "pending"})
    flagged = score(safety={"scan": "issues"})
    assert clean > unknown == pending > flagged
    assert unknown - flagged == 45
    assert clean - flagged == 55
    # Hard enough that even the best-looking safetensors file leaves grade A/B.
    best_but_flagged = security.score_entry(entry(
        downloads=10_000_000, safety={"scan": "issues", "publisher_trust": "official",
                                      "hash_published": True}), now=NOW)
    assert best_but_flagged["score"] < 70
    assert best_but_flagged["grade"] not in ("A", "B")
    scan = next(f for f in best_but_flagged["factors"] if f["id"] == "scan")
    assert scan["tone"] == "danger"


def test_a_flagged_pickle_from_nobody_is_graded_unsafe():
    worst = security.score_entry(entry(
        "ckpt", downloads=3, license=None, created_at="2027-01-10T00:00:00Z",
        safety={"scan": "issues", "custom_code": True}), now=NOW)
    assert worst["score"] == 0
    assert (worst["grade"], worst["label"], worst["tone"]) == ("F", "Unsafe", "danger")
    assert unclamped(worst) < 0, "clamped up to zero, never negative"


def test_the_scan_detail_from_the_host_is_shown_verbatim():
    result = security.score_entry(entry(safety={
        "scan": "issues", "scan_detail": "Hugging Face flagged model.ckpt."}), now=NOW)
    scan = next(f for f in result["factors"] if f["id"] == "scan")
    assert scan["detail"] == "Hugging Face flagged model.ckpt."


def test_the_original_publisher_outranks_a_repackager_and_an_unknown():
    official = score(safety={"publisher_trust": "official"})
    trusted = score(safety={"publisher_trust": "trusted"})
    curated = score(safety={"publisher_trust": "curated"})
    unknown = score(safety={"publisher_trust": "unknown"})
    assert official > trusted > curated > unknown
    assert official - unknown == 15


def test_adoption_age_code_hash_and_licence_each_move_the_score():
    base = score(downloads=5_000)
    assert score(downloads=500_000) > score(downloads=50_000) > base
    assert score(downloads=500) < base, "no adoption factor between 100 and 999"
    assert score(downloads=5) < score(downloads=500)
    assert score(downloads=None) == score(downloads=500)

    assert score(created_at="2027-01-10T00:00:00Z") < score(created_at=None) < score()

    assert score(safety={"custom_code": True}) < score(safety={"code_files": 2}) < score()
    assert score(safety={"hash_published": True}) == score() + 5

    assert score(license="apache-2.0") > score(license="flux-1-dev-non-commercial") \
        > score(license=None)
    assert score(license="unknown") == score(license=None)


def test_an_unparsable_date_is_ignored_rather_than_raised():
    assert score(created_at="last tuesday") == score(created_at=None)


# -------------------------------------------------------------------- bounds

def _every_combination():
    formats = ("safetensors", "gguf", "ckpt", "bin", "", "exe")
    scans = ("clean", "issues", "pending", "unknown", None)
    trusts = ("official", "trusted", "curated", "unknown", None)
    downloads = (None, 0, 50, 5_000, 50_000, 5_000_000)
    extras = ({"created_at": OLD, "license": "mit",
               "safety": {"hash_published": True}},
              {"created_at": "2027-01-10T00:00:00Z", "license": None,
               "safety": {"custom_code": True}},
              {"created_at": None, "license": "other", "safety": {"code_files": 3}})
    for fmt, scan, trust, count, extra in itertools.product(
            formats, scans, trusts, downloads, extras):
        safety = {"scan": scan, "publisher_trust": trust, **extra["safety"]}
        yield entry(fmt, downloads=count, created_at=extra["created_at"],
                    license=extra["license"], safety=safety)


def test_the_score_is_always_between_0_and_99_and_never_100():
    seen = set()
    for item in _every_combination():
        result = security.score_entry(item, now=NOW)
        assert isinstance(result["score"], int)
        assert 0 <= result["score"] <= security.MAX_SCORE == 99
        assert result["score"] != 100
        assert result["score"] == max(0, min(99, unclamped(result)))
        seen.add(result["score"])
    assert 0 in seen and 99 in seen, "both clamps are actually exercised"


def test_the_best_possible_entry_is_capped_at_99():
    best = security.score_entry(entry(
        "safetensors", downloads=50_000_000, license="apache-2.0",
        safety={"scan": "clean", "publisher_trust": "official",
                "hash_published": True}), now=NOW)
    assert unclamped(best) > 99
    assert best["score"] == 99
    assert best["grade"] == "A"


def test_every_factor_is_a_complete_readable_line():
    for item in _every_combination():
        result = security.score_entry(item, now=NOW)
        assert result["factors"], "a score with no explanation is not allowed"
        ids = [f["id"] for f in result["factors"]]
        assert len(ids) == len(set(ids)), "one line per fact"
        for factor in result["factors"]:
            assert set(factor) == FACTOR_KEYS
            assert isinstance(factor["points"], int)
            assert factor["label"] and factor["detail"]
            assert factor["tone"] in TONES
            if factor["points"] > 0:
                assert factor["tone"] == "ok"
            elif factor["points"] == 0:
                assert factor["tone"] == "neutral"
            else:
                assert factor["tone"] in ("warn", "danger")


def test_the_factor_points_plus_the_base_reproduce_the_score():
    """With no clamping in play the number can be read straight back."""
    checked = 0
    for item in _every_combination():
        result = security.score_entry(item, now=NOW)
        total = unclamped(result)
        if 0 <= total <= security.MAX_SCORE:
            assert result["score"] == total
            checked += 1
    assert checked > 500

    plain = security.score_entry(entry(), now=NOW)
    assert {f["id"]: f["points"] for f in plain["factors"]} == {
        "format": 25, "scan": 0, "publisher": 0, "adoption": 2, "age": 3, "license": 4}
    assert plain["score"] == security.BASE_SCORE + 25 + 2 + 3 + 4 == 74


def test_every_result_says_it_is_an_estimate_and_not_a_scan():
    result = security.score_entry(entry(), now=NOW)
    assert set(result) == {"score", "grade", "label", "tone", "factors",
                           "disclaimer", "inferred"}
    assert result["inferred"] is True
    assert result["disclaimer"] == security.DISCLAIMER
    assert "not a malware scan" in security.DISCLAIMER
    local = security.score_local({"format": "safetensors"})
    assert local["inferred"] is True
    assert local["disclaimer"] == security.DISCLAIMER


@pytest.mark.parametrize("value,grade,label,tone", [
    (99, "A", "Safe", "ok"), (85, "A", "Safe", "ok"),
    (84, "B", "Low risk", "ok"), (70, "B", "Low risk", "ok"),
    (69, "C", "Caution", "warn"), (50, "C", "Caution", "warn"),
    (49, "D", "Risky", "danger"), (30, "D", "Risky", "danger"),
    (29, "F", "Unsafe", "danger"), (0, "F", "Unsafe", "danger"),
    (-5, "F", "Unsafe", "danger"),
])
def test_grades(value, grade, label, tone):
    assert security.grade_for(value) == {"grade": grade, "label": label, "tone": tone}


def test_an_empty_entry_is_scored_without_raising():
    result = security.score_entry({}, now=NOW)
    assert 0 <= result["score"] <= 99
    assert result["score"] == unclamped(result)


# -------------------------------------------------------- files already here

def local(**fields) -> dict:
    return security.score_local(fields)


def test_a_local_pickle_scores_far_below_a_local_safetensors_file():
    safe = local(format="safetensors", integrity="ok", hash_state="done")
    pickle = local(format="ckpt", integrity="ok", hash_state="done")
    assert safe["score"] - pickle["score"] == 50
    factor = next(f for f in pickle["factors"] if f["id"] == "format")
    assert factor["points"] == -25
    assert "ComfyUI will unpickle" in factor["detail"]
    assert pickle["grade"] in ("D", "F")


def test_a_local_integrity_problem_is_a_heavy_named_penalty():
    good = local(format="safetensors", integrity="ok")
    bad = local(format="safetensors", integrity="truncated_header")
    assert good["score"] - bad["score"] == 28, "+8 for a clean header against -20"
    factor = next(f for f in bad["factors"] if f["id"] == "integrity")
    assert factor["points"] == -20
    assert factor["tone"] == "danger"
    assert "truncated header" in factor["detail"]


def test_a_civitai_hash_match_is_the_strongest_local_provenance():
    matched = local(format="safetensors", civitai_state="matched", hash_state="done")
    not_found = local(format="safetensors", civitai_state="not_found", hash_state="done")
    hashed = local(format="safetensors", hash_state="done")
    unhashed = local(format="safetensors")
    assert matched["score"] > hashed["score"] > not_found["score"] > unhashed["score"]
    provenance = next(f for f in matched["factors"] if f["id"] == "provenance")
    assert (provenance["label"], provenance["points"]) == ("Matches a published file", 15)
    assert matched["score"] == security.BASE_SCORE + 25 + 8 + 15 == 88
    assert matched["grade"] == "A"


def test_a_recorded_source_adds_a_little_and_an_unknown_format_costs_a_little():
    assert local(format="safetensors", download_url="https://civitai.com/models/1")["score"] \
        == local(format="safetensors")["score"] + 2
    assert local(format="zip")["score"] == local(format="safetensors")["score"] - 30


def test_local_scores_stay_in_bounds_and_add_up():
    for fmt, integrity, civitai, hashed, url in itertools.product(
            ("safetensors", ".gguf", "ckpt", "pt", "", None),
            ("ok", "corrupt", None),
            ("matched", "not_found", "none", None),
            ("done", "pending", None),
            ("https://civitai.com/models/1", None)):
        result = local(format=fmt, integrity=integrity, civitai_state=civitai,
                       hash_state=hashed, download_url=url)
        assert 0 <= result["score"] <= 99
        assert result["score"] == max(0, min(99, unclamped(result)))
        for factor in result["factors"]:
            assert set(factor) == FACTOR_KEYS
            assert factor["tone"] in TONES
