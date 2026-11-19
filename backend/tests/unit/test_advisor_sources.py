"""One normaliser per catalog source, each fed a hand-written document.

Nothing here opens a socket: the normalisers in ``advisor/sources.py`` are pure
functions over already-fetched JSON, so each fixture below is a small dict shaped
like the real API response, carrying only the fields the normaliser reads.

The second half pins the **URL builders**.  They are the only place a value from
the source list reaches a URL, so every URL they can produce must pass the
catalog allowlist, and a hostile value must be dropped or refused - never
spliced into the query string.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

import pytest

from app.advisor import catalog, sources
from app.enable import hosts

GIB = sources.GIB
MIB = sources.MIB
SHA = "ab" * 32


def sibling(path: str, size: int, sha: str | None = SHA) -> dict:
    return {"rfilename": path, "size": size, "lfs": {"size": size, "sha256": sha}}


# -------------------------------------------------- Hugging Face: ComfyUI repo

def comfy_repo() -> dict:
    return {
        "id": "Comfy-Org/Demo-Image",
        "pipeline_tag": "text-to-image",
        "tags": ["diffusion-single-file", "comfyui", "license:apache-2.0", "region:us"],
        "downloads": 1234, "likes": 7, "sha": "deadbeef",
        "createdAt": "2026-01-01T00:00:00.000Z",
        "lastModified": "2026-02-01T00:00:00.000Z",
        "securityRepoStatus": {"scansDone": True, "filesWithIssues": []},
        "siblings": [
            sibling("split_files/diffusion_models/demo_fp16.safetensors", 20 * GIB),
            sibling("split_files/diffusion_models/demo_fp8_scaled.safetensors", 10 * GIB),
            sibling("split_files/text_encoders/t5xxl_fp16.safetensors", 9 * GIB),
            sibling("split_files/text_encoders/t5xxl_fp8_scaled.safetensors", 5 * GIB),
            sibling("split_files/vae/demo_vae.safetensors", 300 * MIB),
            sibling("split_files/loras/demo_lightning_lora.safetensors", 600 * MIB),
            {"rfilename": "README.md", "size": 4096},
            {"rfilename": "config.json", "size": 512},
        ],
    }


def test_a_comfyui_repo_becomes_variants_companions_and_save_paths():
    entry = sources.normalize_hf(comfy_repo(), trust={"official": ["black-forest-labs"],
                                                      "trusted": ["Comfy-Org"]})
    assert catalog._valid_entry(entry)
    assert entry["id"] == "hf:Comfy-Org/Demo-Image"
    assert entry["source"] == "huggingface"
    assert entry["name"] == "Demo-Image"
    assert entry["publisher"] == "Comfy-Org"
    assert entry["kind"] == "image"
    assert entry["task"] == "text-to-image"
    assert entry["runtime"] == "comfyui"
    assert entry["url"] == "https://huggingface.co/Comfy-Org/Demo-Image"
    assert entry["license"] == "apache-2.0"
    assert entry["revision"] == "deadbeef"
    assert entry["downloads"] == 1234
    assert entry["tags"] == ["diffusion-single-file", "comfyui"], "namespaced tags dropped"
    assert entry["nsfw"] is False

    # Variants: the diffusion models only, largest first.
    assert [(v["id"], v["precision"], v["bytes"]) for v in entry["variants"]] == [
        ("split_files/diffusion_models/demo_fp16.safetensors", "fp16", 20 * GIB),
        ("split_files/diffusion_models/demo_fp8_scaled.safetensors", "fp8_scaled", 10 * GIB),
    ]
    for variant in entry["variants"]:
        assert variant["format"] == "safetensors"
        assert variant["save_path"] == "models/diffusion_models"
        assert variant["bundled"] is False
        assert variant["direct"] is True
        assert variant["parts"] == 1
        assert variant["sha256"] == SHA
        assert variant["download_url"] == (
            "https://huggingface.co/Comfy-Org/Demo-Image/resolve/main/" + variant["id"])

    # Companions: encoders and the VAE, each with the folder it belongs in.  The
    # optional LoRA is neither a variant nor something the model needs.
    assert [(c["role"], c["label"], c["save_path"]) for c in entry["companions"]] == [
        ("text_encoder", "t5xxl_fp8_scaled", "models/text_encoders"),
        ("text_encoder", "t5xxl_fp16", "models/text_encoders"),
        ("vae", "demo_vae", "models/vae"),
    ]
    assert entry["safety"] == {"scan": "clean", "scan_detail": None,
                               "publisher_trust": "trusted", "code_files": 0,
                               "custom_code": False, "hash_published": True}


def test_publisher_trust_comes_from_the_recognised_lists_only():
    doc = comfy_repo()
    assert sources.normalize_hf(doc)["safety"]["publisher_trust"] == "unknown"
    assert sources.normalize_hf(doc, trust={"official": ["Comfy-Org"]})[
        "safety"]["publisher_trust"] == "official"
    # A lookalike owner is a different owner.
    assert sources.normalize_hf(doc, trust={"official": ["comfy-org", "Comfy-Org2"]})[
        "safety"]["publisher_trust"] == "unknown"


def test_a_pin_hint_supplies_the_name_kind_and_curation():
    entry = sources.normalize_hf(comfy_repo(), hint={
        "name": "Demo Image", "kind": "video", "family": "Demo", "curated": True,
        "quality": 91, "note": "Hand-written note."})
    assert entry["name"] == "Demo Image"
    assert entry["kind"] == "video"
    assert entry["family"] == "Demo"
    assert entry["curated"] is True
    assert entry["quality"] == 91
    assert entry["description"] == "Hand-written note."


def test_the_host_scan_status_is_carried_into_the_entry():
    doc = comfy_repo()
    doc["securityRepoStatus"] = {"scansDone": False, "filesWithIssues": []}
    assert sources.normalize_hf(doc)["safety"]["scan"] == "pending"
    doc["securityRepoStatus"] = {"scansDone": True, "filesWithIssues": [
        {"path": "model.ckpt", "level": "unsafe"}]}
    safety = sources.normalize_hf(doc)["safety"]
    assert safety["scan"] == "issues"
    assert safety["scan_detail"] == "Hugging Face flagged model.ckpt."
    # A "caution"-level note is not a flag.
    doc["securityRepoStatus"] = {"scansDone": True, "filesWithIssues": [
        {"path": "x.bin", "level": "caution"}]}
    assert sources.normalize_hf(doc)["safety"]["scan"] == "clean"
    del doc["securityRepoStatus"]
    assert sources.normalize_hf(doc)["safety"]["scan"] == "unknown"


def test_scripts_and_the_custom_code_tag_are_counted_never_run():
    doc = comfy_repo()
    doc["siblings"] += [{"rfilename": "pipeline.py", "size": 100},
                        {"rfilename": "nodes/extra.py", "size": 100}]
    doc["tags"].append("custom_code")
    safety = sources.normalize_hf(doc)["safety"]
    assert safety["code_files"] == 2
    assert safety["custom_code"] is True


def test_the_not_for_all_audiences_tag_marks_the_entry():
    doc = comfy_repo()
    doc["tags"].append("not-for-all-audiences")
    assert sources.normalize_hf(doc)["nsfw"] is True


@pytest.mark.parametrize("patch", [
    {"private": True},
    {"disabled": True},
    {"id": "../etc/passwd"},
    {"id": "no-slash"},
    {"id": ""},
    {"siblings": []},
    {"siblings": [{"rfilename": "README.md", "size": 10}]},
    # Weight-shaped names that are too small to be weights.
    {"siblings": [sibling("model.safetensors", 1024)]},
    # Training leftovers are not models.
    {"siblings": [sibling("optimizer.pt", 3 * GIB), sibling("scheduler.bin", 3 * GIB)]},
])
def test_a_repo_that_is_private_disabled_or_holds_no_weights_is_not_an_entry(patch):
    doc = comfy_repo()
    doc.update(patch)
    assert sources.normalize_hf(doc) is None


# ---------------------------------------------------- Hugging Face: GGUF repo

def gguf_repo() -> dict:
    return {
        "id": "bartowski/Demo-7B-GGUF",
        "pipeline_tag": "text-generation",
        "tags": ["gguf", "base_model:demo-labs/Demo-7B",
                 "base_model:quantized:demo-labs/Demo-7B", "license:mit"],
        "gguf": {"total": 7_000_000_000, "context_length": 32768,
                 "architecture": "llama"},
        "siblings": [
            sibling("Demo-7B-Q8_0.gguf", int(7.5 * GIB)),
            sibling("Demo-7B-Q4_K_M.gguf", int(4.4 * GIB)),
            sibling("Demo-7B-IQ3_XS.gguf", int(3.1 * GIB)),
            sibling("Demo-7B.imatrix.gguf", GIB),
            sibling("mmproj-Demo-7B-f16.gguf", 800 * MIB),
            sibling("pytorch_model.bin", 14 * GIB),
        ],
    }


def test_a_gguf_repo_lists_its_quantisations_and_keeps_the_projector_apart():
    entry = sources.normalize_hf(gguf_repo())
    assert catalog._valid_entry(entry)
    assert entry["kind"] == "llm"
    assert entry["runtime"] == "gguf"
    assert entry["base_model"] == "demo-labs/Demo-7B"
    assert entry["family"] == "Demo-7B"
    assert entry["license"] == "mit"
    assert entry["params"] == 7_000_000_000
    assert entry["context_length"] == 32768
    assert entry["architecture"] == "llama"
    assert "Context length 32,768 tokens." in entry["description"]

    # Quant labels, largest first; the importance matrix and the stray pickle
    # are not variants of the model.
    assert [(v["id"], v["precision"]) for v in entry["variants"]] == [
        ("Demo-7B-Q8_0.gguf", "Q8_0"),
        ("Demo-7B-Q4_K_M.gguf", "Q4_K_M"),
        ("Demo-7B-IQ3_XS.gguf", "IQ3_XS"),
    ]
    assert all(v["format"] == "gguf" for v in entry["variants"])
    assert all(v["save_path"] is None for v in entry["variants"]), \
        "a language model has no ComfyUI folder"
    assert not any("imatrix" in v["id"] for v in entry["variants"])

    (projector,) = entry["companions"]
    assert projector["role"] == "projector"
    assert projector["label"] == "mmproj-Demo-7B-f16"
    assert projector["precision"] == "F16"
    assert projector["bytes"] == 800 * MIB


def test_base_model_prefers_the_plain_tag_and_ignores_anything_that_is_not_a_repo():
    assert sources.hf_base_model({"tags": ["base_model:quantized:a/b", "base_model:c/d"]}) \
        == "c/d"
    assert sources.hf_base_model({"tags": ["base_model:finetune:a/b"]}) == "a/b"
    assert sources.hf_base_model({"tags": ["base_model:../../etc", "license:mit"]}) is None
    assert sources.hf_base_model({}) is None


# ------------------------------------------------ Hugging Face: diffusers repo

def diffusers_repo() -> dict:
    return {
        "id": "demo-labs/Demo-Diffusers",
        "pipeline_tag": "text-to-image",
        "tags": [],
        "config": {"diffusers": {"_class_name": "DemoPipeline"}},
        "safetensors": {"parameters": {"BF16": 12_000_000_000}, "total": 12_000_000_000},
        "siblings": [
            sibling("transformer/diffusion_pytorch_model-00001-of-00003.safetensors", 9 * GIB),
            sibling("transformer/diffusion_pytorch_model-00002-of-00003.safetensors", 9 * GIB),
            sibling("transformer/diffusion_pytorch_model-00003-of-00003.safetensors", 5 * GIB),
            sibling("text_encoder/model.safetensors", 4 * GIB),
            sibling("vae/diffusion_pytorch_model.safetensors", 160 * MIB),
        ],
    }


def test_a_sharded_diffusers_repo_merges_its_shards_into_one_variant():
    entry = sources.normalize_hf(diffusers_repo())
    assert entry["runtime"] == "diffusers"
    assert entry["architecture"] == "DemoPipeline"
    assert entry["params"] == 12_000_000_000
    (variant,) = entry["variants"]
    assert variant["id"] == "transformer/diffusion_pytorch_model.safetensors"
    assert variant["bytes"] == 23 * GIB, "the three shards, added up"
    assert variant["parts"] == 3
    assert variant["file"] == "transformer/diffusion_pytorch_model-00001-of-00003.safetensors"
    assert variant["label"] == "Diffusers bf16", "precision from the repo's own metadata"
    assert variant["precision"] == "bf16"
    # Three files cannot be one direct link: the folder is linked instead, and
    # no single checksum is claimed for the merged figure.
    assert variant["direct"] is False
    assert variant["download_url"] == \
        "https://huggingface.co/demo-labs/Demo-Diffusers/tree/main/transformer"
    assert variant["sha256"] is None
    assert entry["safety"]["hash_published"] is False
    assert [c["role"] for c in entry["companions"]] == ["text_encoder", "vae"]


def test_a_pickle_twin_is_dropped_when_a_safetensors_twin_exists():
    doc = {"id": "someone/sd-twins", "pipeline_tag": "text-to-image", "tags": [],
           "siblings": [sibling("model.safetensors", 4 * GIB),
                        sibling("model.ckpt", 4 * GIB),
                        sibling("other.ckpt", 4 * GIB)]}
    entry = sources.normalize_hf(doc)
    assert sorted(v["id"] for v in entry["variants"]) == ["model.safetensors", "other.ckpt"]
    assert {v["format"] for v in entry["variants"]} == {"safetensors", "ckpt"}


def test_a_single_file_checkpoint_in_a_classic_pipeline_repo_is_bundled():
    doc = {"id": "demo-labs/sdxl-demo", "pipeline_tag": "text-to-image", "tags": [],
           "config": {"diffusers": {"_class_name": "StableDiffusionXLPipeline"}},
           "siblings": [sibling("sd_xl_demo_1.0.safetensors", 6 * GIB),
                        sibling("unet/diffusion_pytorch_model.fp16.safetensors", 5 * GIB)]}
    entry = sources.normalize_hf(doc)
    bundled = next(v for v in entry["variants"] if v["id"] == "sd_xl_demo_1.0.safetensors")
    assert bundled["bundled"] is True
    assert bundled["save_path"] == "models/checkpoints"


def test_a_lora_repo_is_typed_from_its_adapter_tag():
    doc = {"id": "someone/demo-style-lora", "pipeline_tag": "text-to-image",
           "tags": ["lora", "base_model:adapter:demo-labs/Demo-Image"],
           "siblings": [sibling("demo_style.safetensors", 150 * MIB)]}
    entry = sources.normalize_hf(doc)
    assert entry["kind"] == "lora"
    assert entry["base_model"] == "demo-labs/Demo-Image"
    assert entry["variants"][0]["save_path"] == "models/loras"


def test_a_repo_of_encoders_only_offers_them_as_the_product():
    doc = {"id": "Comfy-Org/demo_text_encoders", "tags": [],
           "siblings": [sibling("text_encoders/t5xxl_fp16.safetensors", 9 * GIB),
                        sibling("text_encoders/t5xxl_fp8_scaled.safetensors", 5 * GIB)]}
    entry = sources.normalize_hf(doc)
    assert entry["kind"] == "text_encoder"
    assert len(entry["variants"]) == 2
    assert entry["companions"] == []
    assert {v["save_path"] for v in entry["variants"]} == {"models/text_encoders"}


@pytest.mark.parametrize("path,role", [
    ("mmproj-model-f16.gguf", "projector"),
    ("text_encoder/model.safetensors", "text_encoder"),
    ("split_files/text_encoders/umt5_xxl_fp8.safetensors", "text_encoder"),
    ("t5xxl_fp16.safetensors", "text_encoder"),
    ("clip_vision/clip_vision_h.safetensors", "vision"),
    ("vae/diffusion_pytorch_model.safetensors", "vae"),
    ("ae.safetensors", "vae"),
    ("split_files/loras/speed_lora.safetensors", "lora"),
    ("controlnet/depth.safetensors", "controlnet"),
    ("upscale_models/4x_demo.safetensors", "upscale"),
    ("checkpoints/demo_aio.safetensors", "checkpoint"),
    ("split_files/diffusion_models/demo_fp8.safetensors", "main"),
    ("transformer/diffusion_pytorch_model.safetensors", "main"),
    ("demo-Q4_K_M.gguf", "main"),
])
def test_file_role(path, role):
    assert sources.file_role(path) == role


@pytest.mark.parametrize("detail,hint,kind", [
    ({"pipeline_tag": "text-to-video"}, None, "video"),
    ({"pipeline_tag": "text-generation"}, None, "llm"),
    ({"pipeline_tag": "text-to-speech"}, None, "audio"),
    ({"pipeline_tag": "image-to-3d"}, None, "3d"),
    ({"pipeline_tag": "something-new"}, None, "other"),
    ({"pipeline_tag": "text-to-image"}, "video", "video"),
    # An adapter or ControlNet tag outranks both the task and the hint.
    ({"pipeline_tag": "text-to-image", "tags": ["LoRA"]}, "image", "lora"),
    ({"pipeline_tag": "text-to-image", "tags": ["controlnet"]}, "image", "controlnet"),
])
def test_hf_kind(detail, hint, kind):
    assert sources.hf_kind(detail, hint) == kind


def test_guess_kind_reads_the_repo_name_tags_and_folders():
    assert sources.guess_kind("Comfy-Org/Wan_2.2_repackaged", [], []) == "video"
    assert sources.guess_kind("someone/kokoro-82m", [], []) == "audio"
    assert sources.guess_kind("someone/4x-esrgan-pack", [], []) == "upscale"
    assert sources.guess_kind("someone/thing", ["flux"], []) == "image"
    assert sources.guess_kind("someone/thing", [], ["diffusion_models/x.safetensors"]) \
        == "image"
    assert sources.guess_kind("someone/thing", [], ["x.safetensors"]) == "other"


# -------------------------------------------------------------------- Civitai

def civitai_file(**overrides) -> dict:
    row = {"name": "demo_style_v2.safetensors", "type": "Model", "primary": True,
           "sizeKB": 223_000.0, "metadata": {"format": "SafeTensor", "fp": "fp16"},
           "hashes": {"SHA256": "ABCDEF" + "0" * 58},
           "pickleScanResult": "Success", "virusScanResult": "Success"}
    row.update(overrides)
    return row


def civitai_lora(**overrides) -> dict:
    doc = {
        "id": 4242, "name": "Demo Style", "type": "LORA", "nsfw": False,
        "description": "<p>A <b>style</b> adapter.</p><script>alert(1)</script>",
        "creator": {"username": "demo_artist"},
        "stats": {"downloadCount": 52_000, "thumbsUpCount": 900},
        "tags": ["style", "illustration"],
        "allowCommercialUse": ["Image", "Rent"],
        "modelVersions": [
            {"id": 9001, "name": "v2.0", "baseModel": "SDXL 1.0",
             "publishedAt": "2026-03-01T10:00:00.000Z",
             "trainedWords": ["demo style"],
             "files": [civitai_file(),
                       {"name": "training_data.zip", "type": "Training Data",
                        "sizeKB": 900_000.0}]},
            {"id": 9000, "name": "v1.0", "baseModel": "SDXL 1.0",
             "publishedAt": "2026-01-01T10:00:00.000Z",
             "files": [civitai_file(name="demo_style_v1.safetensors", sizeKB=110_000.0)]},
        ],
    }
    doc.update(overrides)
    return doc


def test_a_civitai_lora_becomes_one_entry_with_a_variant_per_version():
    entry = sources.normalize_civitai(civitai_lora())
    assert catalog._valid_entry(entry)
    assert entry["id"] == "civitai:4242"
    assert entry["source"] == "civitai"
    assert entry["kind"] == "lora"
    assert entry["publisher"] == "demo_artist"
    assert entry["family"] == entry["base_model"] == "SDXL 1.0"
    assert entry["url"] == "https://civitai.com/models/4242"
    assert entry["downloads"] == 52_000
    assert entry["license"] == "civitai: commercial use allowed"
    assert entry["description"] == "A style adapter."
    assert entry["created_at"] == "2026-03-01T10:00:00.000Z"
    assert entry["safety"]["scan"] == "clean"
    assert entry["safety"]["hash_published"] is True
    assert entry["nsfw"] is False

    first, second = entry["variants"]
    assert first["id"] == "v9001"
    assert first["label"] == "v2.0"
    assert first["format"] == "safetensors"
    assert first["precision"] == "fp16"
    assert first["bytes"] == int(223_000.0 * 1024)
    assert first["file"] == "demo_style_v2.safetensors"
    assert first["download_url"] == "https://civitai.com/api/download/models/9001"
    assert first["sha256"] == "abcdef" + "0" * 58, "lower-cased"
    assert first["save_path"] == "models/loras"
    assert first["bundled"] is False
    assert first["trained_words"] == ["demo style"]
    assert second["id"] == "v9000"


def test_a_civitai_scan_danger_is_reported_as_issues():
    doc = civitai_lora()
    doc["modelVersions"][0]["files"][0]["pickleScanResult"] = "Danger"
    safety = sources.normalize_civitai(doc)["safety"]
    assert safety["scan"] == "issues"
    assert "flagged" in safety["scan_detail"]


def test_a_civitai_scan_that_has_not_finished_is_pending_not_clean():
    doc = civitai_lora()
    doc["modelVersions"][0]["files"][0]["virusScanResult"] = "Pending"
    assert sources.normalize_civitai(doc)["safety"]["scan"] == "pending"


@pytest.mark.parametrize("patch", [
    {"nsfw": True},
    {"poi": True},
    {"minor": True},
    {"id": "4242"},              # ids are integers; a string could carry anything
    {"id": None},
    {"type": "Workflows"},       # not a model type
    {"type": "Poses"},
    {"modelVersions": []},
    {"modelVersions": [{"id": 1, "files": []}]},
    {"modelVersions": [{"id": 1, "files": [{"name": "x.zip", "type": "Archive",
                                            "sizeKB": 10.0}]}]},
])
def test_civitai_rows_that_must_not_enter_the_catalog(patch):
    assert sources.normalize_civitai(civitai_lora(**patch)) is None


def test_a_civitai_checkpoint_is_a_bundled_image_model_or_video_by_its_base():
    image = sources.normalize_civitai(civitai_lora(type="Checkpoint"))
    assert image["kind"] == "image"
    assert image["variants"][0]["bundled"] is True
    assert image["variants"][0]["save_path"] == "models/checkpoints"
    doc = civitai_lora(type="Checkpoint")
    for version in doc["modelVersions"]:
        version["baseModel"] = "Wan Video 14B t2v"
    assert sources.normalize_civitai(doc)["kind"] == "video"


def test_a_civitai_pickle_file_keeps_its_real_format_and_licence_wording():
    doc = civitai_lora(allowCommercialUse="None")
    doc["modelVersions"] = [{"id": 7, "name": "old", "files": [civitai_file(
        name="old_model.ckpt", metadata={"format": "PickleTensor"}, hashes={})]}]
    entry = sources.normalize_civitai(doc)
    assert entry["variants"][0]["format"] == "ckpt"
    assert entry["variants"][0]["sha256"] is None
    assert entry["safety"]["hash_published"] is False
    assert entry["license"] == "civitai: non-commercial"


def test_only_the_four_newest_civitai_versions_are_kept():
    doc = civitai_lora()
    doc["modelVersions"] = [
        {"id": 100 + i, "name": f"v{i}", "files": [civitai_file()]} for i in range(9)]
    assert len(sources.normalize_civitai(doc)["variants"]) == 4


# ----------------------------------------------------- ComfyUI-Manager list

def manager_row(**overrides) -> dict:
    row = {"name": "Demo VAE (fp16)", "type": "VAE", "base": "SDXL",
           "save_path": "default", "description": "A demo VAE.",
           "reference": "https://huggingface.co/demo-labs/demo-vae",
           "filename": "demo_vae_fp16.safetensors",
           "url": "https://huggingface.co/demo-labs/demo-vae/resolve/main/"
                  "demo_vae_fp16.safetensors",
           "size": "319.14MB"}
    row.update(overrides)
    return row


def test_a_manager_row_becomes_a_curated_entry_with_its_folder():
    entry = sources.normalize_comfy_manager(manager_row())
    assert catalog._valid_entry(entry)
    assert entry["id"] == "comfy:models_vae_demo_vae_fp16.safetensors"
    assert entry["source"] == "comfy_manager"
    assert entry["kind"] == "vae"
    assert entry["publisher"] == "demo-labs"
    assert entry["family"] == "SDXL"
    assert entry["curated"] is True
    assert entry["safety"]["publisher_trust"] == "curated"
    assert entry["url"] == "https://huggingface.co/demo-labs/demo-vae"
    (variant,) = entry["variants"]
    assert variant["bytes"] == int(319.14 * MIB), "the size string, parsed"
    assert variant["save_path"] == "models/vae", "'default' resolves by type"
    assert variant["precision"] == "fp16"
    assert variant["format"] == "safetensors"
    assert variant["download_url"] == manager_row()["url"]
    assert variant["bundled"] is False


@pytest.mark.parametrize("save_path,row_type,expected", [
    ("default", "checkpoints", "models/checkpoints"),
    ("default", "lora", "models/loras"),
    ("default", "upscale", "models/upscale_models"),
    ("", "controlnet", "models/controlnet"),
    ("checkpoints/SDXL", "checkpoints", "models/checkpoints/SDXL"),
    ("models/custom/place", "vae", "models/custom/place"),
])
def test_manager_save_paths(save_path, row_type, expected):
    entry = sources.normalize_comfy_manager(manager_row(save_path=save_path,
                                                        type=row_type))
    assert entry["variants"][0]["save_path"] == expected


def test_a_manager_checkpoint_is_bundled_and_video_bases_are_video():
    entry = sources.normalize_comfy_manager(manager_row(type="checkpoints", base="SD1.5"))
    assert entry["kind"] == "image"
    assert entry["variants"][0]["bundled"] is True
    video = sources.normalize_comfy_manager(manager_row(type="diffusion_model",
                                                        base="Wan2.1"))
    assert video["kind"] == "video"


@pytest.mark.parametrize("patch", [
    {"filename": ""},
    {"url": "http://example.test/model.safetensors"},     # not https
    {"url": "ftp://example.test/model.safetensors"},
    {"url": ""},
    {"size": ""},
    {"size": "unknown"},
    {"size": None},
])
def test_manager_rows_without_a_file_an_https_link_or_a_size_are_skipped(patch):
    assert sources.normalize_comfy_manager(manager_row(**patch)) is None


def test_an_unrecognised_manager_type_is_kind_other_which_the_updater_drops():
    assert sources.normalize_comfy_manager(manager_row(type="mystery"))["kind"] == "other"


# ---------------------------------------------------------------- OpenModelDB

def openmodeldb_item(**overrides) -> dict:
    item = {"name": "4x Demo Sharp", "author": ["alice", "bob"], "license": "CC-BY-4.0",
            "architecture": "esrgan", "scale": 4, "date": "2026-04-02",
            "description": "Sharpens <i>photos</i> well.", "tags": ["photo", "restoration"],
            "resources": [
                {"platform": "pytorch", "type": "pth", "size": 66_000_000,
                 "sha256": "c" * 64, "urls": ["https://example.test/4x.pth"]},
                {"platform": "pytorch", "type": "safetensors", "size": 65_900_000,
                 "sha256": "d" * 64,
                 "urls": ["http://insecure.test/4x.safetensors",
                          "https://example.test/4x.safetensors"]},
                {"platform": "onnx", "type": "onnx", "size": 70_000_000,
                 "urls": ["https://example.test/4x.onnx"]},
            ]}
    item.update(overrides)
    return item


def test_an_openmodeldb_record_becomes_an_upscaler_with_safetensors_first():
    entry = sources.normalize_openmodeldb("4x-Demo-Sharp", openmodeldb_item())
    assert catalog._valid_entry(entry)
    assert entry["id"] == "omdb:4x-Demo-Sharp"
    assert entry["source"] == "openmodeldb"
    assert entry["kind"] == "upscale"
    assert entry["task"] == "4x upscale"
    assert entry["scale"] == 4
    assert entry["publisher"] == "alice, bob"
    assert entry["family"] == "esrgan"
    assert entry["url"] == "https://openmodeldb.info/models/4x-Demo-Sharp"
    assert entry["created_at"] == "2026-04-02T00:00:00Z"
    assert entry["description"] == "Sharpens photos well."
    # safetensors is listed first, so it is the lead; onnx is not a weight
    # format this catalog lists.
    assert [v["format"] for v in entry["variants"]] == ["safetensors", "pth"]
    lead = entry["variants"][0]
    assert lead["download_url"] == "https://example.test/4x.safetensors", \
        "the http mirror is never offered"
    assert lead["save_path"] == "models/upscale_models"
    assert lead["sha256"] == "d" * 64
    assert entry["safety"]["hash_published"] is True


def test_two_openmodeldb_resources_of_one_format_get_distinct_variant_ids():
    item = openmodeldb_item(resources=[
        {"platform": "pytorch", "type": "pth", "size": 66_000_000,
         "urls": ["https://example.test/4x-full.pth"]},
        {"platform": "pytorch", "type": "pth", "size": 17_000_000,
         "urls": ["https://example.test/4x-compact.pth"]}])
    entry = sources.normalize_openmodeldb("4x-Demo-Sharp", item)
    ids = [v["id"] for v in entry["variants"]]
    assert len(ids) == len(set(ids)), ids
    assert ids == ["4x-Demo-Sharp.pth", "4x-Demo-Sharp.pth#2"]


@pytest.mark.parametrize("model_id,item", [
    ("../../etc", openmodeldb_item()),
    ("has space", openmodeldb_item()),
    ("", openmodeldb_item()),
    ("ok-id", "not a dict"),
    ("ok-id", openmodeldb_item(resources=[])),
    ("ok-id", openmodeldb_item(resources=[
        {"type": "pth", "size": 10, "urls": ["http://insecure.test/x.pth"]}])),
    ("ok-id", openmodeldb_item(resources=[
        {"type": "pth", "size": "66MB", "urls": ["https://example.test/x.pth"]}])),
    ("ok-id", openmodeldb_item(resources=[
        {"type": "exe", "size": 10, "urls": ["https://example.test/x.exe"]}])),
])
def test_openmodeldb_records_that_are_refused(model_id, item):
    assert sources.normalize_openmodeldb(model_id, item) is None


# --------------------------------------------------------------------- Ollama

def ollama_row(**overrides) -> dict:
    row = {"name": "demo:7b-q4", "size": 4_700_000_000, "digest": "f" * 64,
           "modified_at": "2026-05-01T00:00:00Z", "capabilities": ["completion", "tools"],
           "details": {"family": "demo", "quantization_level": "Q4_K_M"}}
    row.update(overrides)
    return row


def test_an_ollama_tag_becomes_a_language_model_named_by_its_pull_name():
    entry = sources.normalize_ollama(ollama_row())
    assert catalog._valid_entry(entry)
    assert entry["id"] == "ollama:demo:7b-q4"
    assert entry["kind"] == "llm"
    assert entry["runtime"] == "ollama"
    assert entry["pull"] == "ollama pull demo:7b-q4"
    assert entry["family"] == "demo"
    assert entry["url"] == "https://ollama.com/library/demo"
    assert entry["tags"] == ["completion", "tools"]
    (variant,) = entry["variants"]
    assert variant["bytes"] == 4_700_000_000
    assert variant["precision"] == "Q4_K_M"
    assert variant["format"] == "gguf"
    assert variant["direct"] is False, "pulled by name, not downloaded as a file"
    assert entry["safety"]["hash_published"] is True


@pytest.mark.parametrize("patch", [
    {"size": 0},                       # a cloud-hosted model: nothing runs locally
    {"size": -1},
    {"size": "4.7GB"},
    {"size": None},
    {"name": ""},
    {"name": "demo; rm -rf /"},        # the name is shown inside a shell command
    {"name": "demo`id`"},
    {"name": "demo $(id)"},
    {"name": "x" * 81},
])
def test_ollama_rows_that_are_refused(patch):
    assert sources.normalize_ollama(ollama_row(**patch)) is None


# --------------------------------------------------------------- GitHub leads

def github_repo(**overrides) -> dict:
    repo = {"full_name": "demo-labs/demo-video", "html_url":
            "https://github.com/demo-labs/demo-video",
            "description": "A <b>video</b> model.", "stargazers_count": 4200,
            "pushed_at": "2026-10-01T00:00:00Z", "topics": ["text-to-video"],
            "license": {"spdx_id": "Apache-2.0"}, "fork": False, "archived": False}
    repo.update(overrides)
    return repo


def test_a_github_repository_becomes_a_lead_not_a_catalog_entry():
    lead = sources.normalize_github_lead(github_repo())
    assert lead == {"name": "demo-labs/demo-video",
                    "url": "https://github.com/demo-labs/demo-video",
                    "description": "A video model.", "stars": 4200,
                    "updated_at": "2026-10-01T00:00:00Z",
                    "topics": ["text-to-video"], "license": "Apache-2.0"}
    assert not catalog._valid_entry(lead)
    assert sources.normalize_github_lead(github_repo(license=None))["license"] is None


@pytest.mark.parametrize("patch", [
    {"fork": True},
    {"archived": True},
    {"html_url": "https://evil.test/demo-labs/demo-video"},
    {"html_url": "http://github.com/demo-labs/demo-video"},
    {"html_url": "https://github.com.evil.test/x/y"},
    {"html_url": ""},
])
def test_forks_archives_and_foreign_links_are_not_leads(patch):
    assert sources.normalize_github_lead(github_repo(**patch)) is None


# ------------------------------------------------------------ repository ids

@pytest.mark.parametrize("value", [
    "black-forest-labs/FLUX.1-dev", "Comfy-Org/Qwen-Image-2.1", "a/b",
    "user_1/model.v2-final", "0xABC/123",
])
def test_valid_repo_ids(value):
    assert sources.valid_repo_id(value) is True


@pytest.mark.parametrize("value", [
    "", None, "x", "no-slash", "/", "a/", "/b",
    "../x", "a/..", "a/b..c", "..a/b", "./x", "a/./b",
    "a/b/c", "a/b/", "/a/b",
    "a b/c", "a/b c", " a/b", "a/b ", "a/b\nc/d", "a\t/b",
    "https://huggingface.co/a/b", "http://a/b", "//evil.test/a", "a/b?x=1",
    "a/b#frag", "a/b&c", "a@evil.test/b", "a/b%2e%2e", "a:b/c", "a\\b/c",
    "-a/b", ".a/b", "a/-b", "a/.b", "é/b",
    "a" * 97 + "/b", "a/" + "b" * 97,
    123, ["a/b"],
    # ``$`` would match just before one trailing newline; the id is full-matched.
    "a/b\n", "a/b\r\n", "a\n/b",
])
def test_invalid_repo_ids(value):
    assert sources.valid_repo_id(value) is False


def test_a_url_with_a_control_character_is_refused_by_the_allowlist_as_well():
    """Defence in depth behind the id check: the URL itself would be refused."""
    with pytest.raises(hosts.HostNotAllowed) as refused:
        hosts.check(sources.HF_BASE + "/api/models/a/b\n?blobs=true",
                    kind=hosts.KIND_CATALOG)
    assert refused.value.reason == "URL contains control characters"


# --------------------------------------------------------------- URL builders

HOSTILE = ["a&b=c", "x#frag", "@evil.test", "a\nb", "a\r\nHost: evil.test",
           "https://evil.test/", "//evil.test", "a?b", "a=b", "a/b", "a%26b",
           "../..", "a;b", "x" * 65, "\x00"]


def _checked(url: str) -> hosts.CheckedUrl:
    """Every built URL must pass the catalog allowlist exactly as produced."""
    return hosts.check(url, kind=hosts.KIND_CATALOG)


def test_hf_list_url_for_an_ordinary_discovery_query():
    url = sources.hf_list_url({"pipeline_tag": "text-to-image", "sort": "trendingScore",
                               "limit": 14, "kind": "image"})
    assert url == ("https://huggingface.co/api/models?pipeline_tag=text-to-image"
                   "&sort=trendingScore&direction=-1&limit=14")
    assert _checked(url).host == "huggingface.co"
    spaced = sources.hf_list_url({"search": "wan 2.2", "author": "Comfy-Org"})
    assert dict(parse_qsl(urlsplit(spaced).query)) == {
        "author": "Comfy-Org", "search": "wan 2.2", "sort": "downloads",
        "direction": "-1", "limit": "20"}


@pytest.mark.parametrize("key", ["pipeline_tag", "filter", "author", "library", "search"])
@pytest.mark.parametrize("value", HOSTILE)
def test_hf_list_url_drops_a_hostile_value_instead_of_splicing_it_in(key, value):
    url = sources.hf_list_url({key: value})
    parts = urlsplit(url)
    assert _checked(url).host == "huggingface.co"
    assert parts.path == "/api/models"
    assert parts.fragment == ""
    assert dict(parse_qsl(parts.query)) == {"sort": "downloads", "direction": "-1",
                                           "limit": "20"}, "the value is gone entirely"


def test_hf_list_url_bounds_sort_and_limit_and_ignores_unknown_keys():
    url = sources.hf_list_url({"sort": "evil&x=1", "limit": 99_999,
                               "direction": "1", "full": "true", "token": "hf_secret"})
    assert dict(parse_qsl(urlsplit(url).query)) == {"sort": "downloads",
                                                   "direction": "-1", "limit": "60"}
    assert "limit=1" in sources.hf_list_url({"limit": -5})
    with pytest.raises(ValueError):
        sources.hf_list_url({"limit": "20&x=1"})


def test_hf_detail_url_is_built_only_from_a_valid_repository_id():
    url = sources.hf_detail_url("black-forest-labs/FLUX.1-dev")
    assert url == ("https://huggingface.co/api/models/black-forest-labs/FLUX.1-dev"
                   "?blobs=true&securityStatus=true")
    assert _checked(url).host == "huggingface.co"


@pytest.mark.parametrize("repo", [
    "../x", "a/b/c", "a b/c", "https://evil.test/a/b", "a/b?x=1", "a/b#f",
    "a@evil.test/b", "a/b\nc/d", "a/b\n", "", "a/b&c=d",
])
def test_hf_detail_url_refuses_anything_that_is_not_a_repository_id(repo):
    with pytest.raises(ValueError):
        sources.hf_detail_url(repo)


def test_hf_resolve_url_percent_encodes_the_path():
    url = sources.hf_resolve_url("a/b", "split files/x y#1?.safetensors")
    assert url == "https://huggingface.co/a/b/resolve/main/split%20files/x%20y%231%3F.safetensors"
    assert hosts.check(url, kind=hosts.KIND_MODEL).host == "huggingface.co"


def test_civitai_list_url_for_an_ordinary_query():
    url = sources.civitai_list_url({"types": "Checkpoint", "sort": "Highest Rated",
                                    "period": "Week", "limit": 40})
    assert url == ("https://civitai.com/api/v1/models?limit=40&types=Checkpoint"
                   "&sort=Highest%20Rated&period=Week&nsfw=false")
    assert _checked(url).host == "civitai.com"


@pytest.mark.parametrize("key", ["types", "sort", "period"])
@pytest.mark.parametrize("value", HOSTILE)
def test_civitai_list_url_falls_back_to_a_fixed_value_for_hostile_input(key, value):
    url = sources.civitai_list_url({key: value})
    parts = urlsplit(url)
    assert _checked(url).host == "civitai.com"
    assert parts.path == "/api/v1/models"
    assert parts.fragment == ""
    assert dict(parse_qsl(parts.query)) == {
        "limit": "30", "types": "LORA", "sort": "Most Downloaded",
        "period": "Month", "nsfw": "false"}


def test_civitai_list_url_always_asks_for_safe_content_and_bounds_the_limit():
    for query in ({}, {"nsfw": "true"}, {"nsfw": True, "limit": 5000}):
        params = dict(parse_qsl(urlsplit(sources.civitai_list_url(query)).query))
        assert params["nsfw"] == "false"
        assert 1 <= int(params["limit"]) <= 100
    assert "limit=1&" in sources.civitai_list_url({"limit": -4})


def test_github_search_url_for_an_ordinary_query():
    url = sources.github_search_url({"q": "topic:comfyui stars:>200", "limit": 10})
    assert url == ("https://api.github.com/search/repositories"
                   "?q=topic%3Acomfyui%20stars%3A%3E200&sort=stars&order=desc&per_page=10")
    assert _checked(url).host == "api.github.com"
    assert "per_page=30" in sources.github_search_url({"q": "x", "limit": 500})


@pytest.mark.parametrize("value", [*(h for h in HOSTILE if set(h) != {"x"}),
                                   "x" * 121, "", None])
def test_github_search_url_refuses_a_hostile_query_outright(value):
    with pytest.raises(ValueError):
        sources.github_search_url({"q": value})


def test_every_query_in_the_shipped_source_list_builds_an_allowlisted_url():
    doc = catalog._load_json(catalog.SEED_SOURCES)
    discovery = doc["discovery"]
    built = [sources.hf_list_url(q) for q in discovery["huggingface"]]
    built += [sources.civitai_list_url(q) for q in discovery["civitai"]]
    built += [sources.github_search_url(q) for q in discovery["github"]]
    built += [sources.hf_detail_url(p["repo"]) for p in doc["pins"]["huggingface"]]
    assert len(built) > 100
    for url in built:
        assert _checked(url).url == url
        assert urlsplit(url).fragment == ""


def test_the_fixed_endpoints_are_https_and_on_the_catalog_allowlist():
    fixed = {sources.COMFY_MANAGER_LIST: "raw.githubusercontent.com",
             sources.OPENMODELDB_LIST: "openmodeldb.info",
             sources.OLLAMA_TAGS: "ollama.com",
             sources.VAULT_SOURCES: "raw.githubusercontent.com",
             sources.HF_BASE: "huggingface.co",
             sources.CIVITAI_BASE: "civitai.com",
             sources.GITHUB_API: "api.github.com"}
    for url, host in fixed.items():
        assert _checked(url).host == host
    assert {urlsplit(u).hostname for u in fixed} == set(hosts.CATALOG_HOSTS_EXACT), \
        "every allowlisted catalog host is used, and no endpoint is off the list"
    # The app's own source list is pinned to the project repository.
    assert sources.VAULT_SOURCES.startswith(
        "https://raw.githubusercontent.com/GeekatplayStudio/ComfyUI-Asset-Vault/")


def test_requests_identify_the_app_and_nothing_else():
    assert sources.USER_AGENT.startswith("GeekatplayAssetVault/")
    assert sources.USER_AGENT.endswith(" (catalog)")


# -------------------------------------------------------------- small helpers

@pytest.mark.parametrize("value,expected", [
    ("4.71MB", int(4.71 * MIB)),
    ("23.8GB", int(23.8 * GIB)),
    ("1.5 GiB", int(1.5 * GIB)),
    ("512KB", 512 * 1024),
    ("2TB", 2 * 1024 * GIB),
    ("  10 mb ", 10 * MIB),
    ("1024", 1024),
    ("100B", 100),
    (335304388, 335304388),
    (12.9, 12),
    (0, None), (-5, None), ("", None), (None, None),
    ("junk", None), ("12 parsecs", None), ("1.2.3GB", None), ("GB", None),
])
def test_parse_size(value, expected):
    assert sources.parse_size(value) == expected


@pytest.mark.parametrize("filename,expected", [
    ("Demo-7B-Q4_K_M.gguf", "Q4_K_M"),
    ("Demo-7B-Q8_0.gguf", "Q8_0"),
    ("demo-IQ3_XS.gguf", "IQ3_XS"),
    ("demo-q5_k_s.gguf", "Q5_K_S"),
    ("demo.Q6_K.gguf", "Q6_K"),
    ("mmproj-demo-f16.gguf", "F16"),
    ("flux1-dev-fp8.safetensors", "fp8"),
    ("demo_fp8_scaled.safetensors", "fp8_scaled"),
    ("demo_fp8_e4m3fn.safetensors", "fp8_e4m3fn"),
    ("split_files/diffusion_models/demo_bf16.safetensors", "bf16"),
    ("demo-fp16.safetensors", "fp16"),
    ("demo_nf4.safetensors", "nf4"),
    ("demo-bnb-4bit.safetensors", "bnb_4bit"),
    # The shard counter is not a precision, and the last token is the specific one.
    ("model-00001-of-00003.safetensors", None),
    ("demo_fp16-00001-of-00002.safetensors", "fp16"),
    ("demo-fp16-Q4_K_M.gguf", "Q4_K_M"),
    ("plain_model.safetensors", None),
    ("q4evermore.safetensors", None),
    ("", None),
])
def test_precision_of(filename, expected):
    assert sources.precision_of(filename) == expected


def test_clean_text_strips_scripts_tags_and_entities():
    raw = ("<h1>Demo</h1><script>alert('x')</script><style>p{color:red}</style>"
           "<p>Fast &amp; <b>small</b> model.</p>\n\n<img src=x onerror=alert(1)>")
    cleaned = sources.clean_text(raw)
    assert cleaned == "Demo Fast & small model."
    for needle in ("<", ">", "alert", "color:red", "onerror"):
        assert needle not in cleaned


def test_clean_text_removes_a_multi_line_script_block_case_insensitively():
    raw = "Before <SCRIPT type='text/javascript'>\nsteal()\n</SCRIPT> after"
    assert sources.clean_text(raw) == "Before after"


def test_clean_text_truncates_on_a_word_boundary():
    text = "word " * 200
    cleaned = sources.clean_text(text)
    assert cleaned.endswith("...")
    assert len(cleaned) <= sources.MAX_DESCRIPTION + 3
    assert cleaned[:-3].endswith("word"), "never cut mid-word"
    short = sources.clean_text("alpha beta gamma delta", 12)
    assert short == "alpha beta..."
    assert sources.clean_text("short enough") == "short enough"


@pytest.mark.parametrize("value", [None, "", "   ", "<p></p>", "<script>x()</script>", 0])
def test_clean_text_returns_none_when_nothing_readable_is_left(value):
    assert sources.clean_text(value) is None
