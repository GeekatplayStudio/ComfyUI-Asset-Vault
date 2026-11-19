"""System Advisor (API_CONTRACT 22).

Response models are ``Lenient`` on purpose: they pin the fields the interface
relies on and let the rest through, because a hardware profile differs by
platform and a catalog entry differs by source.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .common import Lenient, PageInfo, Strict

ModelKind = Literal["image", "video", "llm", "lora", "audio", "vision", "controlnet",
                    "upscale", "vae", "text_encoder", "embedding", "3d", "other"]
Verdict = Literal["optimal", "good", "max", "offload", "cpu", "base", "no", "unknown"]


class Budget(Lenient):
    backend: str
    unified: bool
    vram_total: int
    vram_safe: int
    vram_max: int
    ram_total: int
    ram_safe: int
    vram_headroom_pct: int
    ram_headroom_pct: int
    inferred: bool = True


class HardwareProfile(Lenient):
    collected_at: int
    os: dict[str, Any]
    cpu: dict[str, Any]
    memory: dict[str, Any]
    gpus: list[dict[str, Any]]
    accelerator: dict[str, Any]
    volumes: list[dict[str, Any]]
    tier: dict[str, Any]
    notes: list[str]
    budget: Budget


class LiveSample(Lenient):
    t: int
    cpu: dict[str, Any]
    memory: dict[str, Any]
    gpus: list[dict[str, Any]]
    disk_io: dict[str, Any]
    net_io: dict[str, Any]
    volumes: list[dict[str, Any]]
    top_processes: list[dict[str, Any]]
    budget: dict[str, Any]
    history: list[dict[str, Any]] | None = None


class CatalogList(Lenient):
    items: list[dict[str, Any]]
    page: PageInfo
    facets: dict[str, list[dict[str, Any]]]
    meta: dict[str, Any]


class CatalogEntry(Lenient):
    id: str
    name: str
    kind: str
    variants: list[dict[str, Any]]
    fit: dict[str, Any]
    security: dict[str, Any]
    budget: Budget


class VerifyLinkRequest(Strict):
    id: str = Field(min_length=3, max_length=240)
    variant: str | None = Field(default=None, max_length=400)


class VerifyLinkResponse(Lenient):
    state: Literal["ok", "gated", "dead", "unreachable", "unchecked"]
    status: int | None = None
    message: str
    url: str | None = None


class Recommendations(Lenient):
    tier: dict[str, Any] | None = None
    budget: Budget
    scope: list[str]
    totals: dict[str, int]
    runnable: int
    catalog_total: int
    groups: list[dict[str, Any]]
    inferred: bool = True


class InstalledList(Lenient):
    items: list[dict[str, Any]]
    page: PageInfo
    facets: dict[str, list[dict[str, Any]]]
    meta: dict[str, Any]


class InstalledDetail(Lenient):
    uid: str
    name: str
    kind: str
    fit: dict[str, Any]
    requirement: dict[str, Any]
    security: dict[str, Any]
    budget: Budget


class SafeConfig(Lenient):
    summary: str
    budget: Budget
    comfyui: dict[str, Any]
    ollama: dict[str, Any]
    llama_cpp: dict[str, Any]
    files: list[dict[str, str]]
    rules: list[str]
    inferred: bool = True


class UpdateStatus(Lenient):
    running: bool
    phase: str | None = None
    phase_label: str | None = None
    done: int = 0
    total: int = 0
    error: str | None = None
    online_enabled: bool
    auto_update: bool
    catalog_origin: str
    catalog_generated_at: int | None = None
    stale: bool
    entries: int


class UpdateRequest(Strict):
    kinds: list[ModelKind] | None = Field(
        default=None, description="Limit the refresh to these model types.")
    sources: list[str] | None = Field(
        default=None, max_length=8, description="Limit the refresh to these sources.")
    auto: bool = Field(
        default=False,
        description="Self-initiated: start only if the catalog is stale, automatic "
                    "refresh is on, and none was attempted in the last six hours. "
                    "Never an error - the returned status says what happened.")


class SourcesReport(Lenient):
    revision: int
    origin: str
    sources: list[dict[str, Any]]
    user_pins: list[dict[str, Any]]
    allowed_hosts: list[str]
    leads: list[dict[str, Any]]
    update: UpdateStatus


class PinRequest(Strict):
    repo: str = Field(min_length=3, max_length=240,
                      description="A Hugging Face repository as owner/name.")
    kind: ModelKind | None = None


class PinResponse(Lenient):
    repo: str
    added_at: int | None = None


class PinDeleted(Lenient):
    removed: bool
    repo: str


class AdvisorSettings(Lenient):
    vram_headroom_pct: int
    ram_headroom_pct: int
    auto_update: bool
    kinds: list[str]
    online_enabled: bool


class AdvisorSettingsPatch(Strict):
    vram_headroom_pct: int | None = Field(default=None, ge=5, le=50)
    ram_headroom_pct: int | None = Field(default=None, ge=5, le=50)
    auto_update: bool | None = None
    kinds: list[ModelKind] | None = None


class Overview(Lenient):
    tier: dict[str, Any] | None = None
    accelerator: dict[str, Any] | None = None
    budget: Budget
    settings: AdvisorSettings
    totals: dict[str, int]
    catalog_total: int
    update: UpdateStatus
