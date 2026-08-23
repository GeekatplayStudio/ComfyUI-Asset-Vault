"""Workflow "Enable" - resolve and fetch a workflow's missing resources (C9).

Layout, and why it is split this way:

``hosts``       the frozen download allowlist and every URL/redirect check (R1, R2)
``placement``   node input name -> ComfyUI model folder, and destination
                derivation inside a configured root (R3)
``sources``     where a download URL may come from: the workflow's own model
                manifest, the Civitai API, the ComfyUI-Manager registry
``report``      the dependency report the user sees before anything is fetched
``plan``        short-lived plan tokens bound to an exact item set (R9)
``download``    streaming fetch, verification, quarantine, resume (R4-R6, R11)
``git_fetch``   node packages: clone or report only.  **The only module in this
                package that may import ``subprocess``**, and the only thing it
                is allowed to run is ``git`` with a frozen argument list (R7, R8)
``service``     the durable job queue, progress bus and cancellation (R11)

Nothing in this package ever runs code that came out of a download.  There is no
``pip install``, no ``requirements.txt`` processing, no ``install.py``, no
post-clone hook - the exact command is *shown to the user* and they decide.
"""

from __future__ import annotations

__all__ = ["download", "git_fetch", "hosts", "placement", "plan", "report", "service"]
