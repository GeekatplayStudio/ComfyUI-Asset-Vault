"""Data tables for architecture detection.  Pure data - no logic lives here.

The canonical vocabulary is frozen in ARCHITECTURE 4.3.1.  Every structural rule
below was checked against the real key sets of ``C:\\ComfyUI\\models``.
"""

from __future__ import annotations

# --- canonical vocabulary (frozen) -------------------------------------------
FAMILIES = (
    "SD1.5", "SD2.x", "SDXL", "SD3", "FLUX.1", "FLUX.2", "Pony", "Illustrious",
    "NoobAI", "Lumina", "HiDream", "Qwen-Image", "WAN", "HunyuanVideo",
    "LTX-Video", "Mochi", "CogVideo", "ACE-Step", "StableAudio", "Hunyuan3D",
    "Cascade", "AuraFlow", "Kolors", "PixArt", "Other", "Unknown",
)

ROLES = (
    "checkpoint", "unet", "vae", "text_encoder", "clip_vision", "controlnet",
    "lora", "embedding", "upscaler", "latent_upscaler", "ipadapter",
    "style_model", "gligen", "hypernetwork", "frame_interpolation", "geometry",
    "detection", "audio_encoder", "other", "unknown",
)

MODALITIES = ("image", "video", "audio", "3d", "multimodal", "text", "unknown")

# --- adapter suffixes (ARCHITECTURE 4.3 layer 2) -----------------------------
ADAPTER_SUFFIXES: tuple[tuple[str, str], ...] = (
    (".lora_A.weight", "peft"), (".lora_B.weight", "peft"),
    (".lora_A.default.weight", "peft"), (".lora_B.default.weight", "peft"),
    (".lora_down.weight", "kohya"), (".lora_up.weight", "kohya"),
    (".lora.down.weight", "diffusers"), (".lora.up.weight", "diffusers"),
    (".hada_w1_a", "loha"), (".hada_w1_b", "loha"),
    (".hada_w2_a", "loha"), (".hada_w2_b", "loha"),
    (".lokr_w1", "lokr"), (".lokr_w2", "lokr"),
    (".lokr_w1_a", "lokr"), (".lokr_w2_a", "lokr"),
    (".oft_blocks", "oft"), (".oft_diag", "oft"),
    (".diff_b", "diff"), (".diff", "diff"),
    (".alpha", "kohya_alpha"),
    (".dora_scale", "dora"),
)

ADAPTER_PREFIXES = (
    "diffusion_model.", "transformer.", "text_encoders.", "lora_unet_", "lora_te_",
    "lora_te1_", "lora_te2_", "model.diffusion_model.", "base_model.model.",
    "unet.", "lycoris_", "lora_", "net.",
)

# --- structural rules --------------------------------------------------------
# Each rule: (name, requires_all, forbids_any, family, role, modality,
#             label, confidence, ambiguous)
# ``requires_all`` / ``forbids_any`` are matched against the set of top-level
# key prefixes plus a set of level-2 prefixes ("a.b") and substring probes
# (entries beginning with "~" are substring probes over the full key text).
Rule = tuple

RULES: tuple[Rule, ...] = (
    # --- bundled checkpoints -------------------------------------------------
    ("flux_bundle", ("model", "text_encoders", "vae"), (),
     "FLUX.1", "checkpoint", "image", "FLUX.1 bundled checkpoint", 0.9, False),
    ("sdxl_ckpt", ("conditioner", "first_stage_model", "model"), (),
     "SDXL", "checkpoint", "image", "SDXL checkpoint (UNet + CLIP-L/G + VAE)", 0.9, False),
    ("sd15_ckpt", ("cond_stage_model", "first_stage_model", "model"), (),
     "SD1.5", "checkpoint", "image", "Stable Diffusion checkpoint (UNet + CLIP + VAE)", 0.75, False),
    ("ltx_av_ckpt", ("audio_vae", "vocoder", "~adaln_single", "~audio_patchify"), (),
     "LTX-Video", "checkpoint", "video",
     "LTX-Video AV checkpoint (transformer + VAE + vocoder)", 0.95, False),
    ("acestep_ckpt", ("audio_vae", "vocoder", "text_embedding_projection"),
     ("~adaln_single", "~audio_patchify"),
     "ACE-Step", "checkpoint", "audio", "ACE-Step audio checkpoint", 0.95, False),
    ("stable_audio", ("conditioner", "model", "pretransform"), (),
     "StableAudio", "checkpoint", "audio", "Stable Audio checkpoint", 0.9, False),
    ("generic_bundle_vae", ("conditioner", "model", "vae"), (),
     "Other", "checkpoint", "image", "Bundled checkpoint (UNet + conditioner + VAE)", 0.6, True),
    ("sd_ckpt_partial", ("first_stage_model", "model"), (),
     "SD1.5", "checkpoint", "image", "Diffusion checkpoint with bundled VAE", 0.55, True),

    # --- controlnets ---------------------------------------------------------
    ("controlnet_sdxl", ("input_blocks", "input_hint_block", "label_emb"), (),
     "SDXL", "controlnet", "image", "SDXL ControlNet", 0.95, False),
    ("controlnet_sd15", ("input_blocks", "input_hint_block"), ("label_emb",),
     "SD1.5", "controlnet", "image", "SD1.5 ControlNet", 0.9, False),
    ("controlnet_flux", ("controlnet_blocks", "controlnet_x_embedder"), (),
     "FLUX.1", "controlnet", "image", "FLUX ControlNet", 0.9, False),
    ("controlnet_diffusers_sdxl", ("controlnet_down_blocks", "controlnet_cond_embedding",
                                   "add_embedding"), (),
     "SDXL", "controlnet", "image", "SDXL ControlNet (diffusers layout)", 0.92, False),
    ("controlnet_diffusers", ("controlnet_down_blocks", "controlnet_cond_embedding"), (),
     "SD1.5", "controlnet", "image", "ControlNet (diffusers layout)", 0.85, False),

    # --- diffusion transformers / UNets --------------------------------------
    ("acestep_dit", ("decoder", "detokenizer", "encoder", "tokenizer"), (),
     "ACE-Step", "unet", "audio", "ACE-Step audio Transformer", 0.9, False),
    ("hidream_o1", ("~model.x_embedder", "~model.language_model"), (),
     "HiDream", "checkpoint", "image", "HiDream O1 image model", 0.9, False),
    ("flux2_dit", ("double_blocks", "single_blocks", "double_stream_modulation_img"), (),
     "FLUX.2", "unet", "image", "FLUX.2 Transformer (dual-stream + modulation)", 0.95, False),
    ("flux1_dit", ("double_blocks", "single_blocks", "img_in", "txt_in", "time_in"), (),
     "FLUX.1", "unet", "image", "FLUX.1 Transformer (dual-stream, BFL layout)", 0.95, False),
    ("flux1_diffusers", ("x_embedder", "context_embedder", "time_text_embed",
                         "transformer_blocks", "single_transformer_blocks"), (),
     "FLUX.1", "unet", "image", "FLUX.1 Transformer (diffusers layout)", 0.9, False),
    ("qwen_image_dit", ("img_in", "txt_norm", "txt_in", "time_text_embed",
                        "transformer_blocks", "proj_out"), (),
     "Qwen-Image", "unet", "image", "Qwen-Image-class MMDiT (dual-stream)", 0.72, True),
    ("qwen_image_adapter", ("transformer_blocks", "~attn.add_k_proj"), ("img_in", "double_blocks"),
     "Qwen-Image", "unet", "image", "Qwen-Image-class MMDiT block stack", 0.7, True),
    ("krea_dit", ("blocks", "tmlp", "tproj", "txtfusion", "txtmlp"), (),
     "Other", "unet", "image", "Krea-class Transformer", 0.85, False),
    ("krea_adapter", ("transformer_blocks", "text_fusion", "time_mod_proj"), (),
     "Other", "unet", "image", "Krea-class Transformer block stack", 0.85, False),
    ("sd3_mmdit", ("joint_blocks",), (),
     "SD3", "unet", "image", "SD3 / MMDiT Transformer", 0.9, False),
    ("sd3_mmdit_diffusers", ("x_embedder", "context_embedder", "t_embedder"), ("double_blocks",),
     "SD3", "unet", "image", "MMDiT Transformer (diffusers layout)", 0.7, True),
    ("wan_animate", ("blocks", "patch_embedding", "text_embedding", "time_embedding",
                     "head", "~motion_encoder"), (),
     "WAN", "unet", "video", "WAN video Transformer (Animate variant)", 0.95, False),
    ("wan_pose", ("blocks", "patch_embedding", "text_embedding", "time_embedding",
                  "head", "~patch_embedding_pose"), (),
     "WAN", "unet", "video", "WAN video Transformer (pose-conditioned)", 0.95, False),
    ("wan_dit", ("blocks", "patch_embedding", "text_embedding", "time_embedding", "head"), (),
     "WAN", "unet", "video", "WAN video Transformer", 0.92, False),
    ("wan_adapter", ("blocks", "~cross_attn"), ("patch_embedding", "double_blocks"),
     "WAN", "unet", "video", "WAN video Transformer block stack", 0.75, True),
    ("lumina_nextdit", ("cap_embedder", "context_refiner", "noise_refiner"), (),
     "Lumina", "unet", "image", "Lumina / NextDiT Transformer", 0.9, True),
    ("nextdit_layers", ("layers", "x_embedder", "~adaLN_modulation"), ("blocks",),
     "Lumina", "unet", "image", "NextDiT-class Transformer (adaLN)", 0.65, True),
    ("nextdit_adapter", ("layers", "~adaLN_modulation"), ("blocks", "transformer_blocks"),
     "Lumina", "unet", "image", "NextDiT-class block stack (adaLN)", 0.6, True),
    ("ltx_av", ("~adaln_single", "~audio_patchify_proj"), (),
     "LTX-Video", "unet", "video", "LTX-Video AV Transformer", 0.9, False),
    ("ltx_dit", ("~adaln_single", "transformer_blocks"), (),
     "LTX-Video", "unet", "video", "LTX-Video Transformer", 0.8, False),
    ("hunyuan_video", ("~double_stream_blocks", "~single_stream_blocks"), (),
     "HunyuanVideo", "unet", "video", "HunyuanVideo Transformer", 0.9, False),
    ("mochi", ("~blocks.0.attn.qkv_x", "~pos_frequencies"), (),
     "Mochi", "unet", "video", "Mochi Transformer", 0.8, False),
    ("cascade", ("~down_blocks", "~clip_txt_mapper", "~previewer"), (),
     "Cascade", "unet", "image", "Stable Cascade stage", 0.75, False),
    ("pixart", ("~adaln_single", "~caption_projection"), (),
     "PixArt", "unet", "image", "PixArt Transformer", 0.7, False),
    ("auraflow", ("~joint_transformer_blocks", "~single_transformer_blocks"), (),
     "AuraFlow", "unet", "image", "AuraFlow Transformer", 0.75, False),
    ("hunyuan3d", ("~volume_decoder", "~geo_decoder"), (),
     "Hunyuan3D", "unet", "3d", "Hunyuan3D shape model", 0.8, False),
    ("sd_unet_only", ("input_blocks", "middle_block", "output_blocks"), ("input_hint_block",),
     "SD1.5", "unet", "image", "Stable Diffusion UNet", 0.7, True),
    ("sd_unet_diffusers", ("down_blocks", "mid_block", "up_blocks", "conv_in", "time_embedding"),
     ("controlnet_down_blocks",),
     "SD1.5", "unet", "image", "Stable Diffusion UNet (diffusers layout)", 0.6, True),

    # --- VAEs (negative-gated: rule 15) --------------------------------------
    ("vae_standalone", ("encoder", "decoder", "~post_quant_conv"),
     ("model", "conditioner", "cond_stage_model", "first_stage_model", "transformer_blocks",
      "blocks", "double_blocks"),
     "Other", "vae", "image", "Variational auto-encoder", 0.85, True),
    ("vae_audio", ("audio_vae", "vocoder"), ("model",),
     "ACE-Step", "vae", "audio", "Audio VAE + vocoder", 0.85, False),
    ("vae_latent_stats", ("encoder", "decoder", "latents_mean", "latents_std"),
     ("model", "conditioner"),
     "Other", "vae", "video", "Video VAE (normalized latents)", 0.8, True),
    ("vae_plain", ("encoder", "decoder"),
     ("model", "conditioner", "cond_stage_model", "first_stage_model", "shared",
      "transformer_blocks", "blocks", "double_blocks", "detokenizer", "tokenizer",
      "backbone", "head", "neck"),
     "Other", "vae", "image", "Encoder/decoder auto-encoder", 0.6, True),

    # --- text encoders -------------------------------------------------------
    ("t5_encoder", ("encoder", "shared"), (),
     "Other", "text_encoder", "text", "T5 text encoder", 0.9, False),
    ("clip_text", ("text_model",), ("vision_model",),
     "Other", "text_encoder", "text", "CLIP text encoder", 0.9, False),
    ("clip_vision", ("vision_model", "visual_projection"), (),
     "Other", "clip_vision", "image", "CLIP vision encoder", 0.9, False),
    ("vlm_encoder", ("language_model", "vision_tower"), (),
     "Other", "text_encoder", "multimodal", "Vision-language text encoder", 0.85, False),
    ("llm_encoder_flat", ("embed_tokens", "layers", "norm"), (),
     "Other", "text_encoder", "text", "LLM text encoder", 0.85, False),
    ("llm_encoder_nested", ("~model.embed_tokens", "~model.layers"), (),
     "Other", "text_encoder", "text", "LLM text encoder", 0.85, False),
    ("llm_encoder_lm", ("~model.language_model", "~model.visual"),
     ("~model.x_embedder", "~x_embedder"),
     "Other", "text_encoder", "multimodal", "Vision-language text encoder", 0.85, False),
    ("text_projection", ("text_embedding_projection",), (),
     "LTX-Video", "text_encoder", "text", "Text-embedding projection head", 0.7, True),
    ("audio_encoder", ("~model.encoder.conv1", "~model.decoder"), (),
     "Other", "audio_encoder", "audio", "Audio encoder", 0.7, True),

    # --- upscalers and utility nets ------------------------------------------
    ("latent_upscaler", ("initial_conv", "res_blocks", "upsampler", "final_conv"), (),
     "Other", "latent_upscaler", "image", "Latent upscaler", 0.9, False),
    ("esrgan", ("~model.1.sub", "~model.0.weight"), (),
     "Other", "upscaler", "image", "ESRGAN-class upscaler", 0.85, False),
    ("esrgan_rrdb", ("~body.0.rdb1", "~conv_first"), (),
     "Other", "upscaler", "image", "RRDB upscaler", 0.85, False),
    ("frame_interp", ("blocks", "encode"), ("head", "patch_embedding"),
     "Other", "frame_interpolation", "video", "Frame-interpolation network", 0.8, False),
    ("optical_flow", ("extract", "fuse", "predict_flow"), (),
     "Other", "frame_interpolation", "video", "Optical-flow / interpolation network", 0.85, False),
    ("geometry_net", ("~model.backbone", "~model.head"), (),
     "Other", "geometry", "3d", "Geometry-estimation network", 0.7, False),
    ("geometry_multi", ("encoder", "points_head", "normal_head"), (),
     "Other", "geometry", "3d", "Geometry-estimation network", 0.85, False),
    ("detector", ("detector", "tracker"), (),
     "Other", "detection", "image", "Detection / tracking model", 0.85, False),
    ("detr", ("backbone", "encoder", "decoder"), ("model",),
     "Other", "detection", "image", "DETR-class detector", 0.8, False),
    ("embedding_ti", ("emb_params",), (),
     "Other", "embedding", "image", "Textual-inversion embedding", 0.9, False),
    ("embedding_sd", ("string_to_param",), (),
     "SD1.5", "embedding", "image", "Textual-inversion embedding", 0.9, False),
    ("ipadapter", ("image_proj", "ip_adapter"), (),
     "Other", "ipadapter", "image", "IP-Adapter", 0.9, False),
    ("style_model", ("style_embedder",), (),
     "Other", "style_model", "image", "Style model", 0.8, False),
    ("taesd", ("~1.weight", "~10.weight"), (),
     "Other", "vae", "image", "Tiny auto-encoder (TAESD-class)", 0.7, False),
)

# --- category directory -> default role / modality ---------------------------
CATEGORY_ROLE = {
    "checkpoints": ("checkpoint", "image"),
    "diffusion_models": ("unet", "image"),
    "unet": ("unet", "image"),
    "diffusers": ("checkpoint", "image"),
    "loras": ("lora", "image"),
    "vae": ("vae", "image"),
    "vae_approx": ("vae", "image"),
    "clip": ("text_encoder", "text"),
    "text_encoders": ("text_encoder", "text"),
    "clip_vision": ("clip_vision", "image"),
    "controlnet": ("controlnet", "image"),
    "embeddings": ("embedding", "image"),
    "upscale_models": ("upscaler", "image"),
    "latent_upscale_models": ("latent_upscaler", "image"),
    "style_models": ("style_model", "image"),
    "gligen": ("gligen", "image"),
    "hypernetworks": ("hypernetwork", "image"),
    "photomaker": ("other", "image"),
    "audio_encoders": ("audio_encoder", "audio"),
    "model_patches": ("other", "image"),
    "frame_interpolation": ("frame_interpolation", "video"),
    "geometry_estimation": ("geometry", "3d"),
    "detection": ("detection", "image"),
    "optical_flow": ("frame_interpolation", "video"),
    "background_removal": ("detection", "image"),
    "LLM": ("text_encoder", "text"),
    "ipadapter": ("ipadapter", "image"),
    "configs": ("other", "unknown"),
}

# --- declared-metadata normalization -----------------------------------------
METADATA_KEYS = (
    "modelspec.architecture", "ss_base_model_version", "ss_sd_model_name",
    "general.architecture", "model_type", "modelspec.title", "architecture",
    "base_model", "model_version",
)

METADATA_MAP: tuple[tuple[str, str], ...] = (
    ("flux-2", "FLUX.2"), ("flux2", "FLUX.2"), ("flux.2", "FLUX.2"),
    ("flux-1", "FLUX.1"), ("flux1", "FLUX.1"), ("flux.1", "FLUX.1"), ("flux", "FLUX.1"),
    ("stable-diffusion-xl", "SDXL"), ("sdxl", "SDXL"), ("sd_xl", "SDXL"),
    ("stable-diffusion-v3", "SD3"), ("sd3", "SD3"), ("stable-diffusion-3", "SD3"),
    ("stable-diffusion-v1", "SD1.5"), ("sd1", "SD1.5"), ("sd_v1", "SD1.5"),
    ("stable-diffusion-v2", "SD2.x"), ("sd2", "SD2.x"),
    ("qwen-image", "Qwen-Image"), ("qwen_image", "Qwen-Image"), ("qwenimage", "Qwen-Image"),
    ("wan2", "WAN"), ("wanvideo", "WAN"), ("wan-video", "WAN"), ("wan_", "WAN"),
    ("hunyuanvideo", "HunyuanVideo"), ("hunyuan-video", "HunyuanVideo"),
    ("hunyuan3d", "Hunyuan3D"),
    ("ltx", "LTX-Video"), ("avtransformer3dmodel", "LTX-Video"),
    ("mochi", "Mochi"), ("cogvideo", "CogVideo"),
    ("acestep", "ACE-Step"), ("ace-step", "ACE-Step"), ("ace_step", "ACE-Step"),
    ("stable-audio", "StableAudio"), ("stableaudio", "StableAudio"),
    ("lumina", "Lumina"), ("nextdit", "Lumina"), ("z_image", "Lumina"), ("z-image", "Lumina"),
    ("hidream", "HiDream"), ("cascade", "Cascade"), ("auraflow", "AuraFlow"),
    ("kolors", "Kolors"), ("pixart", "PixArt"),
    ("illustrious", "Illustrious"), ("noobai", "NoobAI"), ("pony", "Pony"),
)

# --- filename priors (layer 5) ------------------------------------------------
FILENAME_FAMILY: tuple[tuple[str, str], ...] = (
    ("flux2", "FLUX.2"), ("flux-2", "FLUX.2"), ("flux_2", "FLUX.2"),
    ("flux1", "FLUX.1"), ("flux-1", "FLUX.1"), ("flux_1", "FLUX.1"), ("flux", "FLUX.1"),
    ("sdxl", "SDXL"), ("sd_xl", "SDXL"), ("xl_base", "SDXL"), ("xl_refiner", "SDXL"),
    ("sd15", "SD1.5"), ("sd_15", "SD1.5"), ("sd1.5", "SD1.5"), ("v1-5", "SD1.5"),
    ("sd21", "SD2.x"), ("sd_21", "SD2.x"), ("v2-1", "SD2.x"),
    ("sd3", "SD3"), ("sd35", "SD3"), ("sd3.5", "SD3"),
    ("pony", "Pony"), ("illustrious", "Illustrious"), ("noobai", "NoobAI"),
    ("qwen-image", "Qwen-Image"), ("qwen_image", "Qwen-Image"), ("qwenimage", "Qwen-Image"),
    ("qwen-edit", "Qwen-Image"), ("qwen_edit", "Qwen-Image"), ("qwen", "Qwen-Image"),
    ("wan2", "WAN"), ("wan_2", "WAN"), ("wan-2", "WAN"), ("wanvideo", "WAN"),
    ("wananimate", "WAN"), ("wan21", "WAN"), ("wan22", "WAN"),
    ("hunyuanvideo", "HunyuanVideo"), ("hunyuan_video", "HunyuanVideo"),
    ("hunyuan3d", "Hunyuan3D"),
    ("ltx", "LTX-Video"), ("ltxv", "LTX-Video"),
    ("mochi", "Mochi"), ("cogvideo", "CogVideo"),
    ("acestep", "ACE-Step"), ("ace_step", "ACE-Step"), ("ace-step", "ACE-Step"),
    ("stable_audio", "StableAudio"), ("stableaudio", "StableAudio"),
    ("lumina", "Lumina"), ("nextdit", "Lumina"),
    ("z_image", "Lumina"), ("z-image", "Lumina"), ("zit_", "Lumina"),
    ("hidream", "HiDream"), ("cascade", "Cascade"), ("auraflow", "AuraFlow"),
    ("kolors", "Kolors"), ("pixart", "PixArt"),
)

FILENAME_VARIANT: tuple[tuple[str, str], ...] = (
    ("pony", "Pony"), ("illustrious", "Illustrious"), ("noobai", "NoobAI"),
    ("schnell", "schnell"), ("dev", "dev"), ("krea", "Krea"),
    ("i2v", "I2V"), ("t2v", "T2V"), ("animate", "Animate"),
    ("turbo", "Turbo"), ("lightning", "Lightning"), ("distill", "Distilled"),
    ("refiner", "Refiner"), ("inpaint", "Inpaint"),
)

# --- component classification (layer 3b) -------------------------------------
COMPONENT_PREFIXES: tuple[tuple[str, str], ...] = (
    ("first_stage_model", "vae"),
    ("vae", "vae"),
    ("audio_vae", "vae"),
    ("cond_stage_model", "text_encoder"),
    ("conditioner", "text_encoder"),
    ("text_encoders", "text_encoder"),
    ("text_encoder", "text_encoder"),
    ("text_model", "text_encoder"),
    ("vision_model", "clip_vision"),
    ("vision_tower", "clip_vision"),
    ("visual", "clip_vision"),
    ("vocoder", "vocoder"),
    ("detokenizer", "vocoder"),
    ("tokenizer", "text_encoder"),
    ("spiece_model", "text_encoder"),
    ("tokenizer_json", "text_encoder"),
    ("model", "unet"),
    ("diffusion_model", "unet"),
    ("transformer", "unet"),
)

UNET_COMPONENT_KEYS = (
    "double_blocks", "single_blocks", "transformer_blocks", "single_transformer_blocks",
    "joint_blocks", "blocks", "layers", "input_blocks", "output_blocks", "middle_block",
    "down_blocks", "up_blocks", "mid_block", "img_in", "txt_in", "time_in", "x_embedder",
    "patch_embedding", "final_layer", "head", "proj_out", "norm_out", "time_text_embed",
    "context_refiner", "noise_refiner", "cap_embedder", "adaln_single", "net", "core",
    "time_embedding", "text_embedding", "time_projection", "img_emb",
)

# Roles whose "base model family" is not a diffusion family: filename priors are
# skipped for these, and an unmatched family reports 'Other' rather than a guess.
AUXILIARY_ROLES = frozenset({
    "text_encoder", "clip_vision", "audio_encoder", "geometry", "detection",
    "frame_interpolation", "upscaler", "latent_upscaler", "other",
})

MODALITY_BY_FAMILY = {
    "WAN": "video", "HunyuanVideo": "video", "LTX-Video": "video", "Mochi": "video",
    "CogVideo": "video", "ACE-Step": "audio", "StableAudio": "audio",
    "Hunyuan3D": "3d",
}
