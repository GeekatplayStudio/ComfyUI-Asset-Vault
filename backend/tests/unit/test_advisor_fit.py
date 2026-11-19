"""System Advisor memory arithmetic: budget, requirement, verdict.

The property this file exists to hold is the one the feature is named for: the
advisor **never plans a machine to 100%**.  ``safe < max <= total`` on every
budget, the headroom setting is clamped to 5-50%, and asking for more headroom
can only ever make a verdict worse or leave it alone - never better.
"""

from __future__ import annotations

import itertools

import pytest

from app.advisor import fit

GIB = fit.GIB
MIB = fit.MIB


def profile(backend: str, vram: int, ram: int, *, unified: bool = False,
            pooled: int | None = None) -> dict:
    return {"accelerator": {"backend": backend, "vram_total_bytes": vram,
                            "pooled_vram_bytes": pooled if pooled is not None else vram,
                            "unified": unified},
            "memory": {"total_bytes": ram}}


CUDA_24 = profile("cuda", 24 * GIB, 64 * GIB)
CUDA_8 = profile("cuda", 8 * GIB, 16 * GIB)
APPLE_24 = profile("mps", int(24 * GIB * 0.66), 24 * GIB, unified=True)
CPU_ONLY = profile("cpu", 0, 32 * GIB)
PROFILES = {"cuda24": CUDA_24, "cuda8": CUDA_8, "apple24": APPLE_24, "cpu": CPU_ONLY}


# -------------------------------------------------------------------- budget

def test_budget_for_a_24_gb_cuda_card_with_64_gb_of_ram():
    budget = fit.make_budget(CUDA_24)
    assert budget.backend == "cuda"
    assert budget.unified is False
    assert budget.vram_total == 24 * GIB
    assert budget.vram_reserve == fit.DISCRETE_SYSTEM_RESERVE
    assert budget.vram_max == 24 * GIB - fit.DISCRETE_SYSTEM_RESERVE
    # 15% headroom off the whole card, then the system reserve.
    assert budget.vram_safe == int(24 * GIB * 0.85) - fit.DISCRETE_SYSTEM_RESERVE
    assert budget.vram_headroom_pct == fit.DEFAULT_VRAM_HEADROOM_PCT == 15
    assert budget.ram_headroom_pct == fit.DEFAULT_RAM_HEADROOM_PCT == 25
    assert budget.ram_reserve == int(64 * GIB * fit.RAM_SYSTEM_RESERVE_SHARE)
    assert budget.ram_max == 64 * GIB - budget.ram_reserve
    assert budget.ram_safe == int(64 * GIB * 0.75) - budget.ram_reserve
    assert budget.pooled_safe == budget.vram_safe
    assert budget.pooled_max == budget.vram_max


def test_budget_for_an_8_gb_cuda_card():
    budget = fit.make_budget(CUDA_8)
    assert budget.vram_reserve == fit.DISCRETE_SYSTEM_RESERVE
    assert budget.vram_max == 8 * GIB - fit.DISCRETE_SYSTEM_RESERVE
    assert budget.vram_safe == int(8 * GIB * 0.85) - fit.DISCRETE_SYSTEM_RESERVE
    # 12% of 16 GiB is under the 4 GiB floor, so the floor applies.
    assert budget.ram_reserve == fit.RAM_SYSTEM_RESERVE_MIN
    assert budget.ram_safe == 8 * GIB


def test_budget_for_apple_unified_memory_is_one_pool():
    budget = fit.make_budget(APPLE_24)
    assert budget.backend == "mps"
    assert budget.unified is True
    assert budget.vram_reserve == 0, "the system share comes out of RAM, once"
    assert budget.vram_max == min(budget.vram_total, budget.ram_max)
    assert budget.vram_safe == int(budget.vram_max * 0.85)
    assert (budget.pooled_safe, budget.pooled_max) == (budget.vram_safe, budget.vram_max)


def test_a_unified_gpu_share_can_never_exceed_ram_minus_the_system_reserve():
    # A share figure larger than what RAM can spare is capped by RAM.
    budget = fit.make_budget(profile("mps", 15 * GIB, 16 * GIB, unified=True))
    assert budget.vram_max == budget.ram_max == 12 * GIB
    assert budget.vram_safe < budget.vram_max


def test_budget_for_a_cpu_only_machine_has_no_gpu_memory_at_all():
    budget = fit.make_budget(CPU_ONLY)
    assert budget.backend == "cpu"
    assert (budget.vram_total, budget.vram_safe, budget.vram_max,
            budget.pooled_safe, budget.pooled_max, budget.vram_reserve) == (0,) * 6
    assert 0 < budget.ram_safe < budget.ram_max <= budget.ram_total


def test_a_gpu_backend_reporting_no_vram_is_planned_as_cpu():
    budget = fit.make_budget(profile("cuda", 0, 32 * GIB))
    assert budget.backend == "cpu"
    assert budget.vram_max == 0


def test_an_empty_profile_yields_an_all_zero_cpu_budget():
    budget = fit.make_budget({})
    assert budget.backend == "cpu"
    assert budget.ram_total == budget.ram_safe == budget.ram_max == 0


@pytest.mark.parametrize("name", sorted(PROFILES))
@pytest.mark.parametrize("vram_pct", [5, 15, 30, 50])
@pytest.mark.parametrize("ram_pct", [5, 25, 50])
def test_safe_is_below_max_and_max_never_exceeds_what_is_installed(
        name, vram_pct, ram_pct):
    budget = fit.make_budget(PROFILES[name], vram_headroom_pct=vram_pct,
                             ram_headroom_pct=ram_pct)
    assert 0 < budget.ram_safe < budget.ram_max <= budget.ram_total
    if budget.backend == "cpu":
        return
    assert 0 < budget.vram_safe < budget.vram_max <= budget.vram_total
    assert 0 < budget.pooled_safe < budget.pooled_max <= budget.vram_pooled
    # Never 100%: even the ceiling leaves the reserve, and "safe" leaves at
    # least the minimum headroom on top of that.
    assert budget.vram_safe <= budget.vram_total * (1 - fit.MIN_HEADROOM_PCT / 100)


@pytest.mark.parametrize("vram_gib", [1, 2, 4, 6, 8, 12, 16, 24, 48, 96])
def test_no_discrete_card_size_is_ever_planned_to_the_brim(vram_gib):
    budget = fit.make_budget(profile("cuda", vram_gib * GIB, 32 * GIB),
                             vram_headroom_pct=fit.MIN_HEADROOM_PCT)
    assert 0 < budget.vram_safe < budget.vram_max < budget.vram_total
    assert budget.vram_reserve > 0
    # On a very small card the reserve is a quarter of it, not a fixed 0.8 GiB.
    assert budget.vram_reserve <= budget.vram_total // 4


def test_the_pooled_budget_adds_the_other_cards_but_reserves_only_once():
    budget = fit.make_budget(profile("cuda", 24 * GIB, 128 * GIB, pooled=48 * GIB))
    assert budget.vram_pooled == 48 * GIB
    assert budget.pooled_max == 48 * GIB - fit.DISCRETE_SYSTEM_RESERVE
    assert budget.pooled_safe == int(48 * GIB * 0.85) - fit.DISCRETE_SYSTEM_RESERVE
    assert budget.vram_max == 24 * GIB - fit.DISCRETE_SYSTEM_RESERVE


@pytest.mark.parametrize("given,expected", [
    (None, 15), ("", 15), ("abc", 15), ([], 15),     # unusable -> the default
    (0, 5), (-20, 5), (1, 5), (4.4, 5),              # floor
    (5, 5), (15, 15), ("30", 30), (27.6, 28), (50, 50),
    (51, 50), (100, 50), (1e9, 50),                  # ceiling: never 100%
])
def test_headroom_is_clamped_between_5_and_50_percent(given, expected):
    assert fit.clamp_pct(given, 15) == expected
    budget = fit.make_budget(CUDA_24, vram_headroom_pct=given)
    assert budget.vram_headroom_pct == expected
    assert fit.MIN_HEADROOM_PCT <= budget.vram_headroom_pct <= fit.MAX_HEADROOM_PCT


def test_asking_for_zero_headroom_still_leaves_headroom():
    greedy = fit.make_budget(CUDA_24, vram_headroom_pct=0, ram_headroom_pct=0)
    assert greedy.vram_headroom_pct == greedy.ram_headroom_pct == 5
    assert greedy.vram_safe < greedy.vram_max < greedy.vram_total
    assert greedy.ram_safe < greedy.ram_max < greedy.ram_total


def test_more_headroom_shrinks_the_safe_budget_and_leaves_the_ceiling_alone():
    small = fit.make_budget(CUDA_24, vram_headroom_pct=10, ram_headroom_pct=10)
    large = fit.make_budget(CUDA_24, vram_headroom_pct=40, ram_headroom_pct=40)
    assert large.vram_safe < small.vram_safe
    assert large.ram_safe < small.ram_safe
    assert large.vram_max == small.vram_max
    assert large.ram_max == small.ram_max


def test_as_dict_is_flagged_inferred_and_carries_disk_free():
    data = fit.make_budget(CUDA_24, disk_free=500 * GIB).as_dict()
    assert data["inferred"] is True
    assert data["disk_free"] == 500 * GIB
    assert {"backend", "unified", "vram_total", "vram_pooled", "vram_reserve",
            "vram_safe", "vram_max", "pooled_safe", "pooled_max", "ram_total",
            "ram_reserve", "ram_safe", "ram_max", "vram_headroom_pct",
            "ram_headroom_pct", "disk_free", "inferred"} == set(data)


# --------------------------------------------------------------- requirement

REQUIREMENT_KEYS = {"weights_bytes", "overhead_bytes", "companion_bytes",
                    "gpu_bytes", "ram_bytes", "total_bytes", "inferred"}


@pytest.mark.parametrize("kind", fit.KINDS)
def test_every_kind_yields_a_complete_inferred_requirement(kind):
    need = fit.requirement(kind, 6 * GIB)
    assert set(need) == REQUIREMENT_KEYS
    assert need["inferred"] is True
    assert need["weights_bytes"] == 6 * GIB
    assert need["overhead_bytes"] > 0
    assert need["gpu_bytes"] > need["weights_bytes"], "weights alone are never enough"
    assert need["total_bytes"] >= need["gpu_bytes"]


@pytest.mark.parametrize("kind", ["image", "video", "audio", "3d", "vision", "upscale",
                                  "other"])
def test_a_generator_needs_its_weights_plus_the_working_set_of_its_kind(kind):
    fixed, share = fit.WORKING_SET[kind]
    need = fit.requirement(kind, 10 * GIB)
    assert need["overhead_bytes"] == fixed + int(10 * GIB * share)
    assert need["gpu_bytes"] == 10 * GIB + need["overhead_bytes"]
    assert need["ram_bytes"] == 5 * GIB + 2 * GIB
    assert need["companion_bytes"] == 0


def test_video_needs_more_working_memory_than_image_for_the_same_weights():
    assert fit.requirement("video", 10 * GIB)["gpu_bytes"] > \
        fit.requirement("image", 10 * GIB)["gpu_bytes"]


def test_a_language_model_adds_context_overhead_and_keeps_its_projector_loaded():
    plain = fit.requirement("llm", 8 * GIB)
    assert plain["overhead_bytes"] == fit.LLM_FIXED_OVERHEAD + int(8 * GIB * 0.10)
    assert plain["gpu_bytes"] == 8 * GIB + plain["overhead_bytes"]
    assert plain["total_bytes"] == plain["gpu_bytes"] + int(1.5 * GIB)
    with_projector = fit.requirement(
        "llm", 8 * GIB, companions=[{"role": "projector", "bytes": GIB}])
    assert with_projector["gpu_bytes"] == plain["gpu_bytes"] + GIB
    # Unquantised transformers weights carry a larger runtime overhead.
    assert fit.requirement("llm", 8 * GIB, runtime="transformers")["overhead_bytes"] \
        > plain["overhead_bytes"]


@pytest.mark.parametrize("kind", sorted(fit.ADDON_KINDS))
def test_an_add_on_costs_only_its_own_small_overhead(kind):
    need = fit.requirement(kind, 300 * MIB)
    assert need["overhead_bytes"] == fit.ADDON_OVERHEAD
    assert need["gpu_bytes"] == 300 * MIB + fit.ADDON_OVERHEAD
    assert need["total_bytes"] == need["gpu_bytes"]
    assert need["ram_bytes"] == 300 * MIB


def test_only_the_lightest_companion_of_each_role_is_counted():
    companions = [{"role": "text_encoder", "bytes": 9 * GIB},
                  {"role": "text_encoder", "bytes": 5 * GIB},
                  {"role": "vae", "bytes": 300 * MIB},
                  {"role": "vae", "bytes": 0},          # unknown size: ignored
                  {"bytes": 100 * MIB}]                  # no role: counted as "other"
    need = fit.requirement("image", 12 * GIB, companions=companions)
    assert need["companion_bytes"] == 5 * GIB + 300 * MIB + 100 * MIB
    # Encoders are swapped around the sampler, so they do not add to GPU need...
    assert need["gpu_bytes"] == fit.requirement("image", 12 * GIB)["gpu_bytes"]
    # ...but half of them counts where everything shares one pool.
    assert need["total_bytes"] == need["gpu_bytes"] + need["companion_bytes"] // 2
    assert need["ram_bytes"] == 6 * GIB + 2 * GIB


def test_a_huge_encoder_beside_a_small_model_sets_the_gpu_need():
    need = fit.requirement("image", GIB, companions=[{"role": "text_encoder",
                                                     "bytes": 9 * GIB}])
    assert need["gpu_bytes"] == 9 * GIB + int(0.5 * GIB)
    assert need["ram_bytes"] == 9 * GIB + 2 * GIB


def test_a_bundled_checkpoint_ignores_companions():
    companions = [{"role": "text_encoder", "bytes": 9 * GIB}]
    bundled = fit.requirement("image", 6 * GIB, companions=companions, bundled=True)
    assert bundled["companion_bytes"] == 0
    assert bundled == fit.requirement("image", 6 * GIB)


def test_unknown_kinds_and_bad_sizes_are_handled_without_raising():
    assert fit.requirement("hologram", 4 * GIB) == fit.requirement("other", 4 * GIB)
    for size in (0, -5, None):
        assert fit.requirement("image", size)["weights_bytes"] == 0


# ------------------------------------------------------------------ verdicts

ASSESS_KEYS = {"verdict", "label", "runs", "speed", "need_bytes", "safe_bytes",
               "ceiling_bytes", "used_pct_of_safe", "used_pct_of_ceiling",
               "headroom_bytes", "disk_ok", "reasons", "inferred"}


def verdict(kind: str, gib: float, budget: fit.Budget, **kwargs) -> dict:
    return fit.assess(kind, fit.requirement(kind, int(gib * GIB), **kwargs), budget)


@pytest.mark.parametrize("kind,gib,expected,speed", [
    ("image", 6, "optimal", "fast"),
    ("image", 15, "good", "fast"),
    ("image", 17.5, "max", "fast"),
    ("video", 17, "max", "moderate"),      # heavy kinds at the ceiling slow down
    ("image", 23, "offload", "slow"),
    ("image", 60, "no", "none"),
    ("llm", 12, "optimal", "fast"),
    ("llm", 30, "offload", "slow"),
    ("llm", 80, "no", "none"),
    ("lora", 1, "base", "none"),
    ("controlnet", 2.5, "base", "none"),
    ("image", 0, "unknown", "none"),
])
def test_the_verdict_ladder_on_a_24_gb_cuda_card(kind, gib, expected, speed):
    result = verdict(kind, gib, fit.make_budget(CUDA_24))
    assert result["verdict"] == expected
    assert result["speed"] == speed
    assert result["label"] == fit.VERDICT_LABEL[expected]
    assert result["runs"] is (expected in fit.RUNS)
    assert set(result) == ASSESS_KEYS
    assert result["inferred"] is True
    assert result["reasons"], "every verdict explains itself"


@pytest.mark.parametrize("kind,gib,expected,speed", [
    ("image", 2, "cpu", "very_slow"),
    ("llm", 4, "cpu", "slow"),
    ("image", 30, "no", "none"),
    ("llm", 40, "no", "none"),
    ("lora", 0.2, "base", "none"),
    ("llm", 0, "unknown", "none"),
])
def test_the_verdict_ladder_on_a_cpu_only_machine(kind, gib, expected, speed):
    result = verdict(kind, gib, fit.make_budget(CPU_ONLY))
    assert result["verdict"] == expected
    assert result["speed"] == speed
    if expected in ("cpu", "no"):
        # With no GPU the figures shown are system-memory figures.
        budget = fit.make_budget(CPU_ONLY)
        assert result["safe_bytes"] == budget.ram_safe
        assert result["ceiling_bytes"] == budget.ram_max


@pytest.mark.parametrize("kind,gib,expected", [
    ("image", 6, "optimal"),
    ("image", 9.5, "good"),
    ("image", 12.5, "max"),
    ("image", 15, "offload"),
    ("image", 30, "no"),
    ("llm", 4, "optimal"),
    ("llm", 11, "good"),
    ("llm", 13, "max"),
    ("llm", 16, "offload"),
    ("llm", 40, "no"),
])
def test_the_verdict_ladder_on_apple_unified_memory(kind, gib, expected):
    result = verdict(kind, gib, fit.make_budget(APPLE_24))
    assert result["verdict"] == expected
    if expected == "offload":
        assert result["speed"] == "very_slow"
    elif expected in fit.ON_GPU:
        assert result["speed"] == "moderate", "mps is capable but behind cuda"


def test_every_verdict_in_the_vocabulary_is_reachable():
    reached = set()
    for budget, kind, gib in itertools.product(
            (fit.make_budget(CUDA_24), fit.make_budget(CPU_ONLY)),
            ("image", "llm", "lora"),
            (0, 1, 6, 15, 17.5, 23, 30, 60, 80)):
        reached.add(verdict(kind, gib, budget)["verdict"])
    assert reached == set(fit.VERDICT_ORDER)
    assert set(fit.VERDICT_LABEL) == set(fit.VERDICT_ORDER)
    assert fit.COMFORTABLE < fit.ON_GPU < fit.RUNS < set(fit.VERDICT_ORDER)


def test_on_unified_memory_the_encoders_count_against_the_same_pool():
    companions = [{"role": "text_encoder", "bytes": 6 * GIB}]
    need = fit.requirement("image", 8 * GIB, companions=companions)
    discrete = fit.assess("image", need, fit.make_budget(
        profile("cuda", 16 * GIB, 64 * GIB)))
    unified = fit.assess("image", need, fit.make_budget(
        profile("mps", 16 * GIB, 64 * GIB, unified=True)))
    assert discrete["need_bytes"] == need["gpu_bytes"]
    assert unified["need_bytes"] == need["total_bytes"] > need["gpu_bytes"]


def test_tight_system_memory_downgrades_optimal_and_says_why():
    # A roomy GPU beside very little RAM: the GPU fits, the load path does not.
    budget = fit.make_budget(profile("cuda", 24 * GIB, 8 * GIB))
    result = verdict("image", 6, budget)
    assert result["verdict"] == "good"
    assert any("System memory is the tighter limit" in r for r in result["reasons"])


def test_a_language_model_can_use_every_card_but_a_diffusion_model_only_one():
    budget = fit.make_budget(profile("cuda", 24 * GIB, 128 * GIB, pooled=48 * GIB))
    assert verdict("llm", 30, budget)["verdict"] == "optimal"
    assert verdict("image", 30, budget)["verdict"] == "offload"


def test_disk_space_is_reported_only_when_both_figures_are_known():
    need = fit.requirement("image", 6 * GIB)
    roomy = fit.make_budget(CUDA_24, disk_free=100 * GIB)
    full = fit.make_budget(CUDA_24, disk_free=GIB)
    unknown = fit.make_budget(CUDA_24)
    assert fit.assess("image", need, roomy, download_bytes=6 * GIB)["disk_ok"] is True
    short = fit.assess("image", need, full, download_bytes=6 * GIB)
    assert short["disk_ok"] is False
    assert any("free disk space" in r for r in short["reasons"])
    assert short["verdict"] == "optimal", "disk space is a note, not a memory verdict"
    assert fit.assess("image", need, unknown, download_bytes=6 * GIB)["disk_ok"] is None
    assert fit.assess("image", need, roomy)["disk_ok"] is None


def test_the_percentages_and_headroom_describe_the_same_numbers():
    budget = fit.make_budget(CUDA_24)
    result = verdict("image", 15, budget)
    assert result["safe_bytes"] == budget.vram_safe
    assert result["ceiling_bytes"] == budget.vram_max
    assert result["headroom_bytes"] == budget.vram_safe - result["need_bytes"]
    assert result["used_pct_of_safe"] == round(
        result["need_bytes"] / budget.vram_safe * 100, 1)
    assert result["used_pct_of_ceiling"] < result["used_pct_of_safe"] <= 100


HEADROOMS = (5, 10, 15, 20, 30, 40, 50)
SIZES_GIB = (0.5, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 14, 15, 16, 17, 17.5, 18, 19, 20,
             22, 24, 28, 32, 40, 48, 64, 96)


@pytest.mark.parametrize("name", sorted(PROFILES))
@pytest.mark.parametrize("kind", ["image", "video", "llm", "audio", "upscale"])
def test_more_headroom_never_improves_a_verdict(name, kind):
    """Monotonic in both settings: stricter headroom -> same verdict or a worse one."""
    for gib in SIZES_GIB:
        need = fit.requirement(kind, int(gib * GIB))
        for fixed in HEADROOMS:
            by_vram = [fit.verdict_rank(fit.assess(kind, need, fit.make_budget(
                PROFILES[name], vram_headroom_pct=pct,
                ram_headroom_pct=fixed))["verdict"]) for pct in HEADROOMS]
            assert by_vram == sorted(by_vram), (name, kind, gib, "vram", fixed, by_vram)
            by_ram = [fit.verdict_rank(fit.assess(kind, need, fit.make_budget(
                PROFILES[name], vram_headroom_pct=fixed,
                ram_headroom_pct=pct))["verdict"]) for pct in HEADROOMS]
            assert by_ram == sorted(by_ram), (name, kind, gib, "ram", fixed, by_ram)


@pytest.mark.parametrize("name", sorted(PROFILES))
@pytest.mark.parametrize("kind", ["image", "video", "llm"])
def test_a_larger_file_of_the_same_model_never_gets_a_better_verdict(name, kind):
    budget = fit.make_budget(PROFILES[name])
    ranks = [fit.verdict_rank(verdict(kind, gib, budget)["verdict"])
             for gib in SIZES_GIB]
    assert ranks == sorted(ranks), (name, kind, ranks)


def test_verdict_rank_follows_the_published_order():
    assert [fit.verdict_rank(v) for v in fit.VERDICT_ORDER] == list(range(8))
    assert fit.verdict_rank("nonsense") == len(fit.VERDICT_ORDER)


@pytest.mark.parametrize("precision,low", [
    ("Q2_K", True), ("Q3_K_M", True), ("IQ3_XS", True), ("iq2_xxs", True),
    ("IQ1_S", True), ("TQ1_0", True),
    ("Q4_K_M", False), ("Q4_0", False), ("IQ4_XS", False), ("Q5_K_M", False),
    ("Q8_0", False), ("F16", False), ("fp8", False), ("bf16", False),
    (None, False), ("", False),
])
def test_low_quant_means_under_four_bits(precision, low):
    assert fit.low_quant(precision) is low


# ------------------------------------------------------------------- entries

def test_an_entry_leads_with_the_largest_comfortable_variant():
    entry = {"kind": "image", "variants": [
        {"id": "fp16", "bytes": int(22.2 * GIB)},     # offload
        {"id": "fp8", "bytes": 12 * GIB},             # optimal  <- optimum
        {"id": "q4", "bytes": 7 * GIB},               # optimal
        {"id": "big", "bytes": int(17.5 * GIB)},      # max      <- maximum
        {"id": "huge", "bytes": 30 * GIB},            # offload  <- stretch
    ]}
    result = fit.assess_entry(entry, fit.make_budget(CUDA_24))
    by_id = {v["id"]: v["verdict"] for v in result["variants"]}
    assert by_id == {"fp16": "offload", "fp8": "optimal", "q4": "optimal",
                     "big": "max", "huge": "offload"}
    assert [v["id"] for v in result["variants"]] == ["fp16", "fp8", "q4", "big", "huge"]
    assert result["optimum_variant"] == "fp8"
    assert result["maximum_variant"] == "big"
    assert result["stretch_variant"] == "huge"
    assert result["lead_variant"] == "fp8"
    assert result["verdict"] == "optimal"
    assert result["runs"] is True
    assert result["inferred"] is True
    for variant in result["variants"]:
        assert variant["requirement"]["inferred"] is True
        assert variant["download_bytes"] >= variant["requirement"]["weights_bytes"]


def test_stretch_is_only_offered_when_it_is_bigger_than_the_maximum():
    entry = {"kind": "image", "variants": [{"id": "a", "bytes": 12 * GIB},
                                          {"id": "b", "bytes": 6 * GIB}]}
    result = fit.assess_entry(entry, fit.make_budget(CUDA_24))
    assert result["optimum_variant"] == "a"
    assert result["maximum_variant"] == "a"
    assert result["stretch_variant"] is None


def test_with_nothing_comfortable_the_entry_leads_with_its_maximum():
    entry = {"kind": "image", "variants": [{"id": "only", "bytes": int(17.5 * GIB)}]}
    result = fit.assess_entry(entry, fit.make_budget(CUDA_24))
    assert result["optimum_variant"] is None
    assert result["maximum_variant"] == "only"
    assert result["lead_variant"] == "only"
    assert result["verdict"] == "max"


def test_an_offload_only_entry_names_the_offloading_variant_as_its_maximum():
    entry = {"kind": "image", "variants": [{"id": "only", "bytes": 30 * GIB}]}
    result = fit.assess_entry(entry, fit.make_budget(CUDA_24))
    assert result["verdict"] == "offload"
    assert result["maximum_variant"] == "only"
    assert result["stretch_variant"] is None


def test_an_entry_too_large_in_every_variant_does_not_run():
    entry = {"kind": "image", "variants": [{"id": "x", "bytes": 80 * GIB},
                                          {"id": "y", "bytes": 70 * GIB}]}
    result = fit.assess_entry(entry, fit.make_budget(CUDA_24))
    assert result["verdict"] == "no"
    assert result["runs"] is False
    assert result["optimum_variant"] is None
    assert result["maximum_variant"] is None
    assert result["lead_variant"] == "y", "the smaller of two that do not fit"


def test_an_llm_optimum_prefers_four_bits_or_better_over_a_larger_crushed_file():
    entry = {"kind": "llm", "variants": [
        {"id": "Q8", "bytes": int(8.5 * GIB), "precision": "Q8_0"},     # offload
        {"id": "Q3", "bytes": int(4.2 * GIB), "precision": "Q3_K_M"},   # good, <4 bit
        {"id": "Q4", "bytes": int(3.9 * GIB), "precision": "Q4_K_S"},   # optimal
        {"id": "Q2", "bytes": 3 * GIB, "precision": "Q2_K"},
    ]}
    result = fit.assess_entry(entry, fit.make_budget(profile("cuda", 8 * GIB, 32 * GIB)))
    assert {v["id"]: v["verdict"] for v in result["variants"]} == {
        "Q8": "offload", "Q3": "good", "Q4": "optimal", "Q2": "optimal"}
    assert result["optimum_variant"] == "Q4", "not the larger Q3"
    assert result["maximum_variant"] == "Q3", "maximum is by size, whatever the bits"
    assert result["stretch_variant"] == "Q8"


def test_an_llm_with_only_low_bit_variants_still_gets_an_optimum():
    entry = {"kind": "llm", "variants": [
        {"id": "Q3", "bytes": 4 * GIB, "precision": "Q3_K_M"},
        {"id": "Q2", "bytes": 3 * GIB, "precision": "Q2_K"}]}
    result = fit.assess_entry(entry, fit.make_budget(profile("cuda", 8 * GIB, 32 * GIB)))
    assert result["optimum_variant"] == "Q3"


def test_the_low_bit_rule_applies_to_language_models_only():
    entry = {"kind": "image", "variants": [
        {"id": "Q3", "bytes": 5 * GIB, "precision": "Q3_K_M"},
        {"id": "Q4", "bytes": 4 * GIB, "precision": "Q4_K_S"}]}
    result = fit.assess_entry(entry, fit.make_budget(CUDA_24))
    assert result["optimum_variant"] == "Q3"


def test_an_add_on_entry_is_a_base_verdict_that_counts_as_usable():
    entry = {"kind": "lora", "variants": [{"id": "v1", "bytes": 200 * MIB}]}
    result = fit.assess_entry(entry, fit.make_budget(CUDA_24))
    assert result["verdict"] == "base"
    assert result["runs"] is True
    assert result["speed"] == "none"
    assert result["optimum_variant"] is None
    assert result["maximum_variant"] is None


def test_an_entry_without_variants_or_sizes_is_unknown():
    budget = fit.make_budget(CUDA_24)
    empty = fit.assess_entry({"kind": "image", "variants": []}, budget)
    assert empty["verdict"] == "unknown"
    assert empty["lead_variant"] is None
    assert empty["variants"] == []
    unsized = fit.assess_entry({"kind": "image", "variants": [{"id": "x"}]}, budget)
    assert unsized["verdict"] == "unknown"
    assert unsized["runs"] is False


def test_a_cpu_only_machine_names_whatever_runs_as_the_maximum():
    entry = {"kind": "llm", "variants": [
        {"id": "big", "bytes": 40 * GIB, "precision": "Q8_0"},
        {"id": "small", "bytes": 4 * GIB, "precision": "Q4_K_M"}]}
    result = fit.assess_entry(entry, fit.make_budget(CPU_ONLY))
    assert result["verdict"] == "cpu"
    assert result["optimum_variant"] is None, "nothing is comfortable without a GPU"
    assert result["maximum_variant"] == "small"
    assert result["stretch_variant"] is None


def test_the_download_figure_adds_the_lightest_companion_of_each_role():
    entry = {"kind": "image",
             "companions": [{"role": "text_encoder", "bytes": 9 * GIB},
                            {"role": "text_encoder", "bytes": 5 * GIB},
                            {"role": "vae", "bytes": 300 * MIB}],
             "variants": [{"id": "split", "bytes": 12 * GIB},
                          {"id": "aio", "bytes": 17 * GIB, "bundled": True}]}
    result = fit.assess_entry(entry, fit.make_budget(CUDA_24))
    split, aio = result["variants"]
    assert split["download_bytes"] == 12 * GIB + 5 * GIB + 300 * MIB
    assert aio["download_bytes"] == 17 * GIB
    assert aio["requirement"]["companion_bytes"] == 0
