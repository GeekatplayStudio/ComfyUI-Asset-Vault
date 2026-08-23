"""Optional local Ollama enrichment.

Prose only - Ollama is **never** used for embeddings (DECISIONS C2).  Every
entry point honours the ``online_enabled``/``ollama_enabled`` kill-switch and
degrades to a clear "unavailable" answer rather than raising.
"""

from __future__ import annotations

import logging

from ..core import config_service

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 3.0
GENERATE_TIMEOUT = 60.0
MAX_PROMPT = 12_000


class OllamaService:
    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url

    @property
    def base_url(self) -> str:
        if self._base_url:
            return self._base_url.rstrip("/")
        return (config_service.get_config().ollama_url or "http://localhost:11434").rstrip("/")

    @property
    def enabled(self) -> bool:
        cfg = config_service.get_config()
        return bool(cfg.ollama_enabled)

    def _client(self, timeout: float):
        import httpx

        return httpx.AsyncClient(timeout=timeout)

    async def check_connection(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "Ollama is disabled in settings."
        try:
            async with self._client(CONNECT_TIMEOUT) as client:
                res = await client.get(f"{self.base_url}/api/tags")
            if res.status_code == 200:
                return True, "Connected to Ollama"
            return False, f"Ollama returned HTTP {res.status_code}"
        except ImportError:
            return False, "httpx is not installed."
        except Exception as exc:  # noqa: BLE001 - offline is a normal state
            return False, f"Ollama is unreachable at {self.base_url}: {exc}"[:300]

    async def list_models(self) -> list[str]:
        if not self.enabled:
            return []
        try:
            async with self._client(5.0) as client:
                res = await client.get(f"{self.base_url}/api/tags")
            if res.status_code != 200:
                return []
            data = res.json()
        except Exception as exc:  # noqa: BLE001
            log.debug("Ollama model list failed: %s", exc)
            return []
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return []
        return [str(m.get("name")) for m in models
                if isinstance(m, dict) and m.get("name")]

    async def status(self) -> dict:
        ok, message = await self.check_connection()
        cfg = config_service.get_config()
        return {
            "enabled": cfg.ollama_enabled, "available": ok, "reason": None if ok else message,
            "url": self.base_url, "model": cfg.ollama_model,
            "models": await self.list_models() if ok else [],
        }

    async def generate(self, prompt: str, model: str | None = None) -> dict:
        if not self.enabled:
            return {"ok": False, "reason": "Ollama is disabled in settings.", "text": None}
        cfg = config_service.get_config()
        model = model or cfg.ollama_model or "llama3.2"
        try:
            async with self._client(GENERATE_TIMEOUT) as client:
                res = await client.post(
                    f"{self.base_url}/api/generate",
                    json={"model": model, "prompt": prompt[:MAX_PROMPT], "stream": False},
                )
            if res.status_code != 200:
                return {"ok": False, "reason": f"Ollama returned HTTP {res.status_code}",
                        "text": None}
            data = res.json()
        except ImportError:
            return {"ok": False, "reason": "httpx is not installed.", "text": None}
        except Exception as exc:  # noqa: BLE001 - offline degrades, never raises
            return {"ok": False, "reason": f"Ollama request failed: {exc}"[:300],
                    "text": None}
        text = data.get("response") if isinstance(data, dict) else None
        return {"ok": bool(text), "text": (text or "").strip() or None,
                "model": model, "reason": None if text else "Empty response"}

    async def describe_asset(self, kind: str, name: str, facts: str,
                             model: str | None = None) -> dict:
        prompt = (
            "You are a concise assistant for a local ComfyUI asset manager.\n"
            f"Describe this {kind} in at most three sentences: what it does, what it is "
            "good for, and any notable caveat. Use only the facts given; never invent "
            "version numbers, ratings, or URLs.\n\n"
            f"Name: {name}\nFacts:\n{facts}\n"
        )
        return await self.generate(prompt, model=model)

    async def summarize_update(self, name: str, current_info: str,
                               release_notes: str, model: str | None = None) -> dict:
        prompt = (
            "Compare an installed ComfyUI asset with newer release notes.\n\n"
            f"Asset: {name}\n\nInstalled:\n{current_info}\n\n"
            f"Release notes:\n{release_notes}\n\n"
            "Answer in three short bullets: (1) is updating recommended, "
            "(2) the concrete benefits, (3) any risk or breaking change. "
            "Use only the information provided."
        )
        return await self.generate(prompt, model=model)


ollama_service = OllamaService()
