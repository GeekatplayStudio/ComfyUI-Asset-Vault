"""MCP prompts and argument completion (MCP_SPEC 7)."""

from __future__ import annotations

import logging

from ..services.queries import models_query, nodes_query, workflows_query
from . import handlers

log = logging.getLogger("vault.mcp")

COMPLETION_CAP = 100

PROMPTS: tuple[dict, ...] = (
    {
        "name": "diagnose_workflow",
        "title": "Diagnose a workflow",
        "description": "Inspect one workflow, list every missing dependency, and say "
                       "exactly what to install to make it runnable.",
        "arguments": [
            {"name": "workflow", "description": "Workflow name or uid.",
             "required": True},
        ],
    },
    {
        "name": "recommend_model",
        "title": "Recommend a model",
        "description": "Given what is actually installed, recommend models for a goal "
                       "and explain the reasoning.",
        "arguments": [
            {"name": "goal", "description": "What the user wants to make.",
             "required": True},
            {"name": "base_model", "description": "Restrict to one base model family.",
             "required": False},
        ],
    },
    {
        "name": "vault_overview",
        "title": "Vault overview",
        "description": "Summarize this ComfyUI installation: scale, what it can do, "
                       "and what is broken.",
        "arguments": [],
    },
    {
        "name": "find_unused",
        "title": "Find unused assets",
        "description": "Which assets are indexed but referenced by no workflow and no "
                       "output?",
        "arguments": [
            {"name": "kind", "description": "model | workflow. Defaults to model.",
             "required": False},
        ],
    },
)

BY_NAME = {p["name"]: p for p in PROMPTS}

#: Completable argument names -> the vocabulary that fills them.
COMPLETABLE = ("workflow", "base_model", "category", "package")


class PromptNotFound(Exception):
    """No prompt with that name."""


def _user(text: str) -> dict:
    return {"role": "user", "content": {"type": "text", "text": text}}


def get(name: str, arguments: dict | None) -> dict:
    arguments = arguments or {}
    prompt = BY_NAME.get(str(name))
    if prompt is None:
        raise PromptNotFound(f"Unknown prompt '{name}'.")

    if name == "diagnose_workflow":
        target = str(arguments.get("workflow") or "").strip()
        if not target:
            raise PromptNotFound("The 'workflow' argument is required.")
        key = "uid" if target.startswith("workflow:") else "name"
        text = (
            f"Call inspect_workflow with {{\"{key}\": \"{target}\"}}. Then report, in "
            "this order:\n"
            "1. What the workflow does (capability tags, base model, node count).\n"
            "2. Every missing model file, with the loader class and input that names "
            "it, and whether a near-miss suggestion looks like the right file.\n"
            "3. Every missing node class with its install_hint package and repo URL, "
            "grouped so the user installs each repo once.\n"
            "4. A single verdict: can it run as-is, and if not, the shortest list of "
            "actions that would make it runnable.\n"
            "Do not modify anything."
        )
        return {"description": prompt["description"], "messages": [_user(text)]}

    if name == "recommend_model":
        goal = str(arguments.get("goal") or "").strip()
        base = str(arguments.get("base_model") or "").strip()
        if not goal:
            raise PromptNotFound("The 'goal' argument is required.")
        base_clause = (f" Restrict to base_model [\"{base}\"]." if base else "")
        text = (
            f"The user wants to: {goal}.\n"
            "Recommend models that are ALREADY INSTALLED in this vault. Start with "
            "vault_stats to see the scale, then vault_search for the goal, then "
            f"list_models to confirm what is present.{base_clause}\n"
            "For each recommendation give: the model name and uid, why it fits the "
            "goal, its base model family and how confident that detection is, its "
            "size, and any workflow already in the vault that uses it. Say plainly "
            "when nothing installed fits, rather than inventing a download. "
            "Do not modify anything."
        )
        return {"description": prompt["description"], "messages": [_user(text)]}

    if name == "vault_overview":
        text = (
            "Summarize this ComfyUI installation for its owner.\n"
            "1. Call vault_stats with breakdown ['category','base_model','modality'].\n"
            "2. Call get_index_status to confirm the numbers are from a completed "
            "scan rather than an empty index.\n"
            "3. Call list_node_packages and list_workflows to characterise what the "
            "install can actually do.\n"
            "Report: overall scale, the strongest model families present, what kinds "
            "of generation this install supports, and a prioritised list of what is "
            "broken (missing files, integrity failures, workflows that cannot run). "
            "Be concrete and quantitative. Do not modify anything."
        )
        return {"description": prompt["description"], "messages": [_user(text)]}

    kind = str(arguments.get("kind") or "model").strip().lower()
    text = (
        f"Find {kind}s that are indexed but referenced by nothing.\n"
        "For models: page through list_models and keep the ones whose "
        "workflow_count and output_count are both 0; confirm a sample with "
        "find_model_usage before reporting them.\n"
        "For workflows: page through list_workflows and keep the ones with no "
        "outputs recorded against them.\n"
        "Report them grouped by category with their sizes and the total disk space "
        "they occupy, largest first. Recommend, but do NOT delete anything: say "
        "explicitly that vault_delete with the default trash mode is the reversible "
        "way to act on the list if the user asks."
    )
    return {"description": prompt["description"], "messages": [_user(text)]}


# ---------------------------------------------------------------------------
# completion/complete
# ---------------------------------------------------------------------------

def _prefix(values, needle: str) -> list[str]:
    needle = str(needle or "").strip().lower()
    seen: list[str] = []
    for v in values:
        text = str(v or "").strip()
        if not text or text in seen:
            continue
        if not needle or needle in text.lower():
            seen.append(text)
    return seen


def complete(argument_name: str, value: str, ctx: handlers.Ctx) -> dict:
    name = str(argument_name or "")
    values: list[str] = []
    try:
        if name == "workflow":
            page = workflows_query.list_workflows({}, sort="name", limit=500,
                                                  conn=ctx.conn)
            values = _prefix((i.get("name") for i in page.items), value)
        elif name == "base_model":
            facets = models_query.model_facets({}, conn=ctx.conn)
            values = _prefix((r.get("value") for r in facets.get("base_model") or []),
                             value)
        elif name == "category":
            facets = models_query.model_facets({}, conn=ctx.conn)
            values = _prefix((r.get("value") for r in facets.get("category") or []),
                             value)
        elif name == "package":
            page = nodes_query.list_node_packages({}, sort="name", limit=500,
                                                  conn=ctx.conn)
            values = _prefix((i.get("display_name") for i in page.items), value)
    except Exception as exc:  # noqa: BLE001 - completion never fails a session
        log.info("completion for %s failed: %s", name, exc)
        values = []
    total = len(values)
    return {"completion": {"values": values[:COMPLETION_CAP],
                           "total": total,
                           "hasMore": total > COMPLETION_CAP}}
