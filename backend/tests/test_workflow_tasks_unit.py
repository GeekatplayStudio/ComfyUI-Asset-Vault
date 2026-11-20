"""What a workflow does - the classifier on its own (schema v11)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.parsers import workflow_tasks as wt  # noqa: E402

T2I = ["CheckpointLoaderSimple", "CLIPTextEncode", "EmptyLatentImage", "KSampler",
       "VAEDecode", "SaveImage"]


def tasks(classes, **kw):
    return wt.classify({c: 1 for c in classes}, **kw)


@pytest.mark.parametrize(("classes", "primary"), [
    (T2I, "text_to_image"),
    (["LoadImage", "VAEEncode", "KSampler", "VAEDecode", "SaveImage"], "image_edit"),
    (["LoadImage", "UpscaleModelLoader", "ImageUpscaleWithModel", "SaveImage"], "upscale"),
    # upscale, then a light img2img refine: still an upscale workflow
    (["LoadImage", "UpscaleModelLoader", "ImageUpscaleWithModel", "VAEEncode", "KSampler",
      "VAEDecode", "SaveImage"], "upscale"),
    (["LoadImage", "BiRefNetRMBG", "SaveImage"], "remove_background"),
    (["LoadImage", "Image Remove Background (rembg)", "SaveImage"], "remove_background"),
    (["LoadImage", "WanImageToVideo", "KSampler", "VAEDecode", "CreateVideo", "SaveVideo"],
     "image_to_video"),
    (["EmptyHunyuanLatentVideo", "KSampler", "VAEDecode", "SaveWEBM"], "text_to_video"),
    (["VHS_LoadVideo", "KSampler", "VHS_VideoCombine"], "video_edit"),
    (["LoadImage", "LoadAudio", "WanVideoSampler", "MultiTalkWav2VecEmbeds",
      "VHS_VideoCombine"], "lip_sync"),
    (["LoadImage", "Hy3DGenerateMesh", "Hy3DExportMesh", "Preview3D"], "three_d"),
    (["TextEncodeAceStepAudio", "EmptyAceStepLatentAudio", "KSampler", "VAEDecodeAudio",
      "SaveAudio"], "audio"),
    (["LoadImage", "PreviewImage"], "utility"),
])
def test_graph_primary_task(classes, primary):
    assert tasks(classes)["primary"] == primary


def test_tasks_are_never_empty_and_main_job_comes_first():
    r = tasks([*T2I, "UpscaleModelLoader", "ImageUpscaleWithModel", "ControlNetLoader"])
    assert r["tasks"][0] == "text_to_image"
    assert {"upscale", "controlnet"} <= set(r["tasks"])
    assert tasks([])["tasks"] == ["utility"]


def test_resize_and_hires_fix_are_not_upscaling():
    r = tasks([*T2I, "ImageScaleBy", "LatentUpscaleBy"])
    assert "upscale" not in r["tasks"]


@pytest.mark.parametrize("cls", ["KlingTextToVideoNode", "IdeogramV3", "OpenAIGPTImage1",
                                 "GeminiImageNode", "ViduTextToVideoNode",
                                 "WanTextToImageApi"])
def test_partner_api_nodes_run_on_api(cls):
    assert tasks(["SaveImage", cls])["runs_on"] == "api"


@pytest.mark.parametrize("cls", ["ImageNoise", "ImageNormalize", "IndividualPrompt",
                                 "KSamplerAdvanced", "FluxGuidance"])
def test_lookalike_class_names_stay_local(cls):
    """"vidu" is inside "individual", "imagen" inside "ImageNoise"."""
    assert tasks([*T2I, cls])["runs_on"] == "local"


def test_api_image_generation_is_text_to_image():
    assert tasks(["IdeogramV3", "SaveImage"])["primary"] == "text_to_image"
    assert tasks(["LoadImage", "FluxKontextProImageNode", "SaveImage"])["primary"] == "image_edit"


def test_subgraph_contents_are_read():
    graph = {"nodes": [{"id": 1, "type": "sg-1"}, {"id": 2, "type": "SaveImage"}],
             "definitions": {"subgraphs": [{"id": "sg-1", "nodes": [
                 {"id": 3, "type": "EmptyLatentImage"}, {"id": 4, "type": "KSampler"}]}]}}
    classes = wt.graph_classes(graph)
    assert {"EmptyLatentImage", "KSampler", "SaveImage"} <= classes
    assert wt.classify(classes)["primary"] == "text_to_image"


def test_template_tags_win_and_category_is_the_fallback():
    r = wt.classify({"LoadImage": 1, "KSampler": 1}, is_template=True,
                    template_tags=["Image Upscale", "API"], template_category="Image Tools",
                    open_source=False)
    assert r["primary"] == "upscale" and r["runs_on"] == "api"
    r = wt.classify({}, is_template=True, template_tags=["Video"],
                    template_category="3D Model", open_source=True)
    assert r["tasks"] == ["three_d"] and r["runs_on"] == "local"
    r = wt.classify({}, is_template=True, template_tags=["Remove Background"])
    assert r["primary"] == "remove_background"


def test_name_hints_choose_among_what_the_graph_shows():
    classes = ["LoadImage", "UpscaleModelLoader", "ImageUpscaleWithModel", "VAEEncode",
               "KSampler", "SaveImage"]
    assert tasks(classes, names=("portrait_inpaint_v2",))["primary"] == "image_edit"
    # a hint for a task the graph does not show changes nothing
    assert tasks(T2I, names=("my_i2v_try",))["tasks"] == ["text_to_image"]
    # but it does replace a bare "utility"
    assert tasks(["MarkdownNote", "SaveImage"], names=("upscale_4x",))["tasks"] == ["upscale"]


def test_lip_sync_is_not_overridden_by_an_i2v_file_name():
    classes = ["LoadImage", "LoadAudio", "WanVideoSampler", "InfiniteTalkModelLoader",
               "VHS_VideoCombine"]
    assert tasks(classes, names=("wanvideo_I2V_InfiniteTalk",))["primary"] == "lip_sync"


def test_vocabulary_is_consistent():
    assert set(wt.PRIMARY_ORDER) == set(wt.TASK_KEYS)
    assert set(wt.TEMPLATE_TAG_TASKS.values()) <= set(wt.TASK_KEYS)
    assert set(wt.TEMPLATE_CATEGORY_TASKS.values()) <= set(wt.TASK_KEYS)
    assert all(t in wt.TASK_KEYS for _w, t in wt.NAME_HINTS)
