"""B3 — base-model detection must be right, and must never contradict itself.

The original defect had three parts:

* only ``tensor_keys[:100]`` were inspected, and safetensors key order is
  arbitrary, so the discriminating keys were usually outside the window
  (``flux1-dev-fp8`` came back as a **VAE**);
* parameter counts summed *every* tensor in a bundled checkpoint, so
  ``flux1-dev`` reported 16.87 B instead of ~12 B; and
* labels were free text, so a model could be family *X* and wear a label naming
  family *Y*.

The fixture is 56 **real** key sets lifted from the owner's install (keys,
shapes, dtypes and header metadata only — no weights), so this runs anywhere
without a 1.5 TB library.  Expected families below are authored from the model's
published identity, independently of what the detector returns.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from app.parsers import arch_detect
from app.parsers import arch_rules as R

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "arch" / "labelled.json.gz"

ACCURACY_FLOOR = 0.92

# Expected family per model, keyed by name.  A tuple means more than one answer
# is defensible — typically where a model is *branded* by one vendor but built
# on another vendor's architecture, and either is a truthful statement about it.
EXPECTED: dict[str, str | tuple[str, ...]] = {
    # --- unambiguous, family is in the published name --------------------
    "flux1-dev-fp8": "FLUX.1",
    "flux-2-klein-4b": "FLUX.2",
    "Flux_2-Turbo-LoRA_comfyui": "FLUX.2",
    "Flux.1-dev-Controlnet-Upscaler": "FLUX.1",
    "flux1_dev_controlnet_canny": "FLUX.1",
    "flux_controlnet_union_pro": "FLUX.1",
    "acestep_v1.5_xl_base_bf16": "ACE-Step",
    "controlnet_tile_sdxl_1_0": "SDXL",
    "control_v1p_sd15_brightness": "SD1.5",
    "control_v1p_sd15_qrcode_monster_v2": "SD1.5",
    "hidream_o1_image_bf16": "HiDream",
    "stable_audio_3_medium": "StableAudio",
    "stable_audio_3_medium_base": "StableAudio",
    "qwen_image_vae": "Qwen-Image",
    "qwen-360-diffusion-2512-int8-bf16-v2": "Qwen-Image",
    "Wan2_1_VAE_bf16": "WAN",
    "wan_2.1_vae": "WAN",
    "taesd3_decoder": "SD3",
    "taesd3_encoder": "SD3",
    "LTX23_video_vae_bf16": "LTX-Video",
    "taeltx2_3": "LTX-Video",
    "ltx-2.3_text_projection_bf16": "LTX-Video",

    # --- branded by one vendor, built on another's architecture ----------
    # Z-Image Turbo is Alibaba's, on a NextDiT/Lumina-class backbone.
    "zImageTurbo_turbo": ("Lumina", "Other"),
    "ZIT_Luneva CyberHD": ("Lumina", "Other"),
    "ZIT_Midjourney_Luneva_Cinematic_v1_r128": ("Lumina", "Other"),
    "pixel_art_style_z_image_turbo": ("Lumina", "Other"),
    # ERNIE is Baidu's; the backbone is NextDiT-class.
    "ernie-image": ("Lumina", "Other"),
    "ernie-image-turbo": ("Lumina", "Other"),
    "mage_flow_bf16": ("Qwen-Image", "Other"),
    # SDPose is a pose estimator built on a Stable Diffusion UNet.
    "sdpose_wholebody_fp16": ("SD2.x", "SD1.5", "Other"),
    # LTX latent upscalers ship beside LTX but are standalone convnets.
    "ltx-2.3-spatial-upscaler-x2-1.0": ("LTX-Video", "Other"),
    "ltx-2.3-spatial-upscaler-x2-1.1": ("LTX-Video", "Other"),
    "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0": ("LTX-Video", "Other"),
    "ltx-2-spatial-upscaler-x2-1.0": ("LTX-Video", "Other"),
    "ltx-2.3-spatial-upscaler-x1.5-1.0": ("LTX-Video", "Other"),
    # TAE-F1 / TAESD are the tiny autoencoders for FLUX.1 / SD1.5.  The
    # architecture is generic, the pairing is not; either answer is honest.
    "taef1_decoder": ("FLUX.1", "Other"),
    "taef1_encoder": ("FLUX.1", "Other"),
    "taesd_decoder": ("SD1.5", "Other"),

    # --- genuinely family-less: encoders, estimators, interpolators ------
    "whisper_large_v3_encoder_fp16": "Other",
    "clip_l": "Other",
    "clip_vision_h": "Other",
    "t5xxl_fp8_e4m3fn": "Other",
    "umt5-xxl-enc-bf16": "Other",
    "byt5_small_glyphxl_fp16": "Other",
    "ernie-image-prompt-enhancer": "Other",
    "pixeldit_1300m_1024px_bf16": "Other",
    "film_net_fp16": "Other",
    "rife_v4.25": "Other",
    "rife_v4.25_heavy": "Other",
    "rife_v4.25_lite": "Other",
    "rife_v4.26": "Other",
    "moge_1_vitl_fp16": "Other",
    "depth_anything_3_base": "Other",
    "depth_anything_3_small": "Other",
    "depth_anything_3_metric_large": "Other",
    "depth_anything_3_mono_large": "Other",
}


def load_fixture() -> list[dict]:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        return json.load(fh)


FIXTURE_ROWS = load_fixture() if FIXTURE.exists() else []


def run(row: dict) -> arch_detect.ArchResult:
    return arch_detect.detect(
        keys=row["keys"], shapes=row["shapes"], dtypes=row["dtypes"],
        metadata=row["metadata"], category=row["category"],
        stem=row["stem"], file_size=row["file_size"])


def test_fixture_is_large_enough():
    assert len(FIXTURE_ROWS) >= 30, "BUILD_PLAN requires a labelled fixture of >= 30 files"
    unlabelled = [r["name"] for r in FIXTURE_ROWS if r["name"] not in EXPECTED]
    assert not unlabelled, f"fixture rows with no authored expectation: {unlabelled}"


def test_family_accuracy_meets_the_floor():
    hits, misses = 0, []
    for row in FIXTURE_ROWS:
        want = EXPECTED[row["name"]]
        want = (want,) if isinstance(want, str) else want
        got = run(row).base_model_family
        if got in want:
            hits += 1
        else:
            misses.append(f"{row['name']} ({row['category']}): got {got}, expected {'|'.join(want)}")
    accuracy = hits / len(FIXTURE_ROWS)
    assert accuracy >= ACCURACY_FLOOR, (
        f"architecture accuracy {accuracy:.1%} < {ACCURACY_FLOOR:.0%}\n" + "\n".join(misses))


def test_nothing_in_the_fixture_is_unknown():
    """``Unknown`` was the pre-fix answer for most loras; it must be gone."""
    unknown = [r["name"] for r in FIXTURE_ROWS if run(r).base_model_family == "Unknown"]
    assert not unknown, f"{len(unknown)} models detected as Unknown: {unknown}"


# ---------------------------------------------------------------------------
# The audit's named cases
# ---------------------------------------------------------------------------

def _row(name: str) -> dict:
    for r in FIXTURE_ROWS:
        if r["name"] == name:
            return r
    pytest.skip(f"{name} not in fixture")


def test_flux1_dev_fp8_is_a_flux_checkpoint_not_a_vae():
    """The headline B3 regression, asserted field by field."""
    res = run(_row("flux1-dev-fp8"))
    assert res.base_model_family == "FLUX.1"
    assert res.model_role == "checkpoint", "was detected as VAE before the fix"
    assert res.is_bundled is True
    assert res.arch_confidence >= 0.9


def test_flux1_dev_param_count_is_the_primary_component_not_the_sum():
    """16.87 B was the whole bundle; the diffusion model alone is ~11.9 B."""
    res = run(_row("flux1-dev-fp8"))
    assert res.param_count_primary is not None
    billions = res.param_count_primary / 1e9
    assert 11.0 <= billions <= 12.5, f"primary params {billions:.2f} B, expected ~11.9 B"
    assert res.param_count_total > res.param_count_primary, (
        "a bundled checkpoint must report both, and total must exceed primary")
    assert res.param_count_total / 1e9 == pytest.approx(16.87, abs=0.2)


def test_acestep_is_detected():
    assert run(_row("acestep_v1.5_xl_base_bf16")).base_model_family == "ACE-Step"


def test_sdxl_controlnet_is_detected():
    res = run(_row("controlnet_tile_sdxl_1_0"))
    assert res.base_model_family == "SDXL"
    assert res.model_role == "controlnet"


def test_loras_are_recognised_as_adapters():
    loras = [r for r in FIXTURE_ROWS if r["category"] == "loras"]
    assert loras
    for row in loras:
        res = run(row)
        assert res.is_adapter, f"{row['name']} not recognised as an adapter"
        assert res.adapter_format, f"{row['name']} has no adapter format"


# ---------------------------------------------------------------------------
# The label/family invariant
# ---------------------------------------------------------------------------

def test_no_label_names_a_family_other_than_its_own():
    """Gate: ``architecture_label`` may never name a family it does not belong to."""
    offenders = []
    for row in FIXTURE_ROWS:
        res = run(row)
        named = arch_detect.label_names_family(res.architecture_label)
        if named is not None and named != res.base_model_family:
            offenders.append(
                f"{row['name']}: family={res.base_model_family} label={res.architecture_label!r}")
    assert not offenders, "labels contradicting their family:\n" + "\n".join(offenders)


def test_enforce_label_family_rewrites_a_contradicting_label():
    res = arch_detect.ArchResult(base_model_family="FLUX.1", model_role="lora",
                                 architecture_label="SDXL block stack LoRA",
                                 is_adapter=True)
    arch_detect.enforce_label_family(res, "loras")
    assert arch_detect.label_names_family(res.architecture_label) != "SDXL"
    assert "LoRA" in res.architecture_label
    assert any("contradicted" in s for s in res.signals)


def test_enforce_label_family_leaves_a_consistent_label_alone():
    res = arch_detect.ArchResult(base_model_family="FLUX.1", model_role="checkpoint",
                                 architecture_label="FLUX.1 bundled checkpoint")
    arch_detect.enforce_label_family(res, "checkpoints")
    assert res.architecture_label == "FLUX.1 bundled checkpoint"
    assert not res.signals


def test_every_detected_family_is_in_the_frozen_vocabulary():
    for row in FIXTURE_ROWS:
        res = run(row)
        assert res.base_model_family in R.FAMILIES, (
            f"{row['name']} -> {res.base_model_family} is outside the frozen vocabulary")
        assert res.model_role in R.ROLES
        assert res.modality in R.MODALITIES


def test_detection_never_reads_only_the_first_100_keys():
    """The literal cause of B3: a truncated key window.

    Shuffling a key set must not change the answer.  Under the old code it did,
    because the discriminating keys moved out of ``tensor_keys[:100]``.
    """
    import random

    row = _row("flux1-dev-fp8")
    baseline = run(row)
    rng = random.Random(99)
    for _ in range(5):
        keys = list(row["keys"])
        rng.shuffle(keys)
        shuffled = dict(row, keys=keys)
        res = run(shuffled)
        assert res.base_model_family == baseline.base_model_family
        assert res.model_role == baseline.model_role
        assert res.param_count_primary == baseline.param_count_primary


def test_detect_never_raises_on_degenerate_input():
    for kwargs in (
        dict(keys=[], shapes={}, dtypes={}, metadata={}, category="", stem=""),
        dict(keys=["x"], shapes={"x": []}, dtypes={"x": "F16"}, metadata={},
             category="loras", stem="x"),
        dict(keys=["a.b.c"], shapes={"a.b.c": [-1, 0]}, dtypes={"a.b.c": "??"},
             metadata={"modelspec.architecture": 5}, category="nope", stem=""),
    ):
        res = arch_detect.detect(**kwargs)
        assert res.base_model_family in R.FAMILIES
