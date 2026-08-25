"""Node packages: clone or report only.  Never execute anything (R7, R8).

This is the **only** module in ``app/enable`` permitted to import
``subprocess``, and the only program it may start is ``git``, with a frozen
argument list, ``shell=False`` and a wall-clock timeout. A remote revision is
captured before the consent plan, then the clone is staged and checked out at
that exact commit before release. Nothing that arrives in the clone is ever run:

* no ``pip install``, no ``python setup.py``, no ``install.py``, no
  ``requirements.txt`` processing, no post-clone hook - not automatically and
  not on request;
* submodules are never fetched, and a ``.gitmodules`` in the result is
  *reported*, never acted on;
* the cloned tree stays subject to the existing AST-only rule for everything
  under ``custom_nodes`` (SECURITY_REVIEW 7.3).

What the user gets instead is the exact command to run themselves, with the
resolved absolute paths already filled in.  Auto-running an untrusted
repository's install steps is remote code execution, and no amount of
convenience buys that back.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess  # one call site, `git` only, frozen argv, shell=False
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..core.errors import ConflictError, ValidationError
from ..core.pathsafe import long_path
from . import hosts

log = logging.getLogger(__name__)

CLONE_TIMEOUT_S = 300
MAX_OUTPUT_CHARS = 4000

#: Files that mean "this package wants to run something on your machine".
#: Presence is reported to the user with the command they would have to run.
INSTALL_MARKERS = ("requirements.txt", "install.py", "setup.py", "pyproject.toml")

#: Frozen ``git`` arguments.  Read this list as the security control it is.
_HARDENING = (
    "-c", "protocol.file.allow=never",     # no file:// remote or submodule
    "-c", "protocol.ext.allow=never",      # no ext:: transport (arbitrary command)
    "-c", "credential.helper=",            # never consult a credential helper
    "-c", "advice.detachedHead=false",
)
_CLONE_FLAGS = (
    "clone",
    "--depth", "1",
    "--single-branch",
    "--no-tags",
    "--no-recurse-submodules",             # `--recurse-submodules` is never passed
)


def resolve_revision(repo_url: str, *, ref: str | None = None,
                     timeout_s: int = 45) -> tuple[str | None, str | None]:
    """Resolve the remote's tip to a commit - HEAD, or one named branch.

    A branch name is mutable.  The exact object id becomes part of a plan and
    clone fails closed if the remote moves before that object can be checked
    out.  This still does not make an unverified legacy mapping official; it
    merely prevents a plan from silently installing a different branch tip.
    The update checker asks the same question with ``ref`` set to a package's
    recorded branch; a ref the remote no longer has falls back to HEAD.
    """
    checked = hosts.check(repo_url, kind=hosts.KIND_GIT)
    git = git_path()
    if git is None:
        return None, "git was not found on PATH"
    target = f"refs/heads/{ref}" if ref else "HEAD"
    argv = [git, *_HARDENING, "ls-remote", "--exit-code", "--", checked.url, target]
    try:
        proc = _run_git(argv, timeout_s=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"could not resolve the remote revision: {exc}"
    if proc.returncode != 0:
        if ref:  # branch renamed or deleted upstream - HEAD is still an answer
            return resolve_revision(repo_url, timeout_s=timeout_s)
        return None, "git could not resolve the remote HEAD"
    for line in (proc.stdout or "").splitlines():
        commit = line.split("\t", 1)[0].strip().lower()
        if len(commit) == 40 and all(ch in "0123456789abcdef" for ch in commit):
            return commit, None
    return None, "the remote did not return an immutable commit id"


@dataclass
class CloneResult:
    state: str                              # done | skipped | failed
    abs_path: str | None = None
    repo_url: str | None = None
    bytes_written: int = 0
    error_code: str | None = None
    error_message: str | None = None
    manual_steps: list[str] = field(default_factory=list)
    findings: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "state": self.state, "abs_path": self.abs_path,
            "repo_url": self.repo_url, "bytes": self.bytes_written,
            "error_code": self.error_code, "error_message": self.error_message,
            "manual_steps": list(self.manual_steps), "findings": dict(self.findings),
            "notes": list(self.notes),
        }


def git_path() -> str | None:
    """The ``git`` executable, or ``None`` when the machine has none."""
    return shutil.which("git")


def available() -> bool:
    return git_path() is not None


def _env() -> dict:
    """A deliberately dull environment: git may not ask anyone anything."""
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_LFS_SKIP_SMUDGE": "1",
    })
    env.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def _run_git(argv: list[str], *, cwd: str | None = None, timeout_s: int) -> subprocess.CompletedProcess:
    """The sole subprocess call site: only a frozen, validated git argv reaches it."""
    return subprocess.run(  # noqa: S603
        argv, cwd=cwd, env=_env(), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", timeout=timeout_s, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def build_argv(git: str, repo_url: str, target: str, hooks_dir: str) -> list[str]:
    """The exact argument vector, exposed so a test can assert its shape."""
    return [
        git,
        "-c", f"core.hooksPath={hooks_dir}",
        *_HARDENING,
        *_CLONE_FLAGS,
        "--", repo_url, target,
    ]


def clone(repo_url: str, target_abs_path: str, *, expected_commit: str | None = None,
          timeout_s: int = CLONE_TIMEOUT_S) -> CloneResult:
    """Stage and release one pinned registry repository.  Runs nothing from it."""
    checked = hosts.check(repo_url, kind=hosts.KIND_GIT)
    git = git_path()
    target = str(target_abs_path)
    if git is None:
        return CloneResult(
            state="failed", repo_url=checked.url, error_code="FEATURE_UNAVAILABLE",
            error_message="git was not found on PATH, so node packages can only be "
                          "reported, not fetched.",
            manual_steps=manual_steps(checked.url, target))
    # R8 first: an existing folder is never touched, whatever else is wrong.
    if os.path.exists(long_path(target)):
        raise ConflictError(
            "A folder with that name already exists in custom_nodes; nothing was "
            "cloned.",
            details={"path": target, "repo_url": checked.url})
    commit = str(expected_commit or "").lower()
    if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
        return CloneResult(state="failed", repo_url=checked.url, error_code="VALIDATION_ERROR",
                           error_message="No immutable commit was captured in the reviewed plan. "
                                         "Request a fresh plan and try again.",
                           manual_steps=manual_steps(checked.url, target))

    parent = os.path.dirname(target)
    try:
        os.makedirs(long_path(parent), exist_ok=True)
    except OSError as exc:
        return CloneResult(state="failed", repo_url=checked.url,
                           error_code="PATH_NOT_ALLOWED",
                           error_message=str(exc)[:300])

    stage_parent = os.path.join(parent, ".vault-staging")
    stage = os.path.join(stage_parent, f"{os.path.basename(target)}-{uuid.uuid4().hex[:12]}")
    try:
        os.makedirs(long_path(stage_parent), exist_ok=True)
    except OSError as exc:
        return CloneResult(state="failed", repo_url=checked.url, error_code="PATH_NOT_ALLOWED",
                           error_message=str(exc)[:300])
    _sweep_stale_staging(stage_parent)
    try:
        with tempfile.TemporaryDirectory(prefix="vault-nohooks-") as hooks_dir:
            argv = build_argv(git, checked.url, stage, hooks_dir)
            try:
                proc = _run_git(argv, cwd=parent, timeout_s=timeout_s)
            except subprocess.TimeoutExpired:
                return CloneResult(
                    state="failed", repo_url=checked.url, error_code="UPSTREAM_UNAVAILABLE",
                    error_message=f"git clone exceeded its {timeout_s}s time limit.",
                    manual_steps=manual_steps(checked.url, target))
            except OSError as exc:
                return CloneResult(state="failed", repo_url=checked.url,
                                   error_code="UPSTREAM_UNAVAILABLE",
                                   error_message=str(exc)[:300],
                                   manual_steps=manual_steps(checked.url, target))

        output = (proc.stdout or "")[:MAX_OUTPUT_CHARS]
        if proc.returncode != 0:
            return CloneResult(
                state="failed", repo_url=checked.url, error_code="UPSTREAM_UNAVAILABLE",
                error_message=f"git clone exited {proc.returncode}: {output.strip()[:300]}",
                manual_steps=manual_steps(checked.url, target))

        checkout = [git, "-C", stage, *_HARDENING, "checkout", "--detach", "--force", commit]
        try:
            verified = _run_git(checkout, timeout_s=timeout_s)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CloneResult(state="failed", repo_url=checked.url, error_code="UPSTREAM_UNAVAILABLE",
                               error_message=f"could not verify planned commit: {exc}"[:300])
        if verified.returncode != 0:
            return CloneResult(state="failed", repo_url=checked.url, error_code="INTEGRITY_MISMATCH",
                               error_message="The repository changed after the plan was reviewed; no package was installed.",
                               manual_steps=manual_steps(checked.url, target))
        findings = inspect_clone(stage)
        try:
            os.replace(long_path(stage), long_path(target))
        except OSError as exc:
            return CloneResult(state="failed", repo_url=checked.url, error_code="PATH_NOT_ALLOWED",
                               error_message=f"could not release verified staged package: {exc}"[:300])
        return CloneResult(
            state="done", abs_path=target, repo_url=checked.url,
            bytes_written=_tree_bytes(target), findings=findings,
            manual_steps=post_clone_steps(target, findings),
            notes=_notes(findings),
        )
    finally:
        # On success os.replace already moved the stage away; on every failure,
        # handled or not, the half-finished clone is removed here.
        _cleanup(stage)


def inspect_clone(target: str) -> dict:
    """Look at what arrived.  Reading, never running."""
    root = Path(target)
    found: dict = {"install_markers": [], "has_gitmodules": False,
                   "submodules": [], "python_requirements": None}
    for name in INSTALL_MARKERS:
        if (root / name).is_file():
            found["install_markers"].append(name)
    gitmodules = root / ".gitmodules"
    if gitmodules.is_file():
        found["has_gitmodules"] = True
        try:
            text = gitmodules.read_text(encoding="utf-8", errors="replace")[:20_000]
        except OSError:
            text = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("url"):
                found["submodules"].append(stripped.split("=", 1)[-1].strip()[:300])
    req = root / "requirements.txt"
    if req.is_file():
        try:
            lines = [ln.strip() for ln in
                     req.read_text(encoding="utf-8", errors="replace")[:16_000]
                     .splitlines()]
        except OSError:
            lines = []
        found["python_requirements"] = [ln for ln in lines
                                        if ln and not ln.startswith("#")][:100]
    return found


def _notes(findings: dict) -> list[str]:
    notes: list[str] = []
    if findings.get("has_gitmodules"):
        notes.append(
            "This repository declares git submodules. They were NOT fetched. "
            "Review them before pulling them in yourself.")
    if findings.get("install_markers"):
        notes.append(
            "This package ships install steps ("
            + ", ".join(findings["install_markers"])
            + "). They were NOT run - running a third-party installer is code "
              "execution, so it stays your decision.")
    return notes


def manual_steps(repo_url: str, target: str) -> list[str]:
    return [f'git clone --depth 1 "{repo_url}" "{target}"']


def post_clone_steps(target: str, findings: dict) -> list[str]:
    """The exact commands the user may choose to run.  We never run them."""
    steps: list[str] = []
    markers = findings.get("install_markers") or []
    python = _comfy_python_hint()
    if "requirements.txt" in markers:
        steps.append(f'"{python}" -m pip install -r "{os.path.join(target, "requirements.txt")}"')
    if "install.py" in markers:
        steps.append(f'"{python}" "{os.path.join(target, "install.py")}"')
    steps.append("Restart ComfyUI, then re-scan the vault.")
    return steps


def _comfy_python_hint() -> str:
    """The interpreter the user's own ComfyUI would use, for the printed command."""
    from ..core import config_service

    cfg = config_service.get_config()
    root = cfg.comfyui_path
    if root is not None:
        for rel in (Path("..") / "python_embeded" / "python.exe",
                    Path("venv") / "Scripts" / "python.exe"):
            candidate = (root / rel).resolve()
            if candidate.is_file():
                return str(candidate)
    return "python"


def _tree_bytes(path: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(long_path(path)):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                continue
    return total


def _cleanup(target: str) -> None:
    """Remove a half-finished clone.  Only ever the directory git was given."""
    if not target or not os.path.isdir(long_path(target)):
        return
    try:
        shutil.rmtree(long_path(target), ignore_errors=True)
    except OSError as exc:
        log.warning("could not clean up a failed clone at %s: %s", target, exc)


def _sweep_stale_staging(stage_parent: str) -> None:
    """Remove staging leftovers from a process that died mid-clone.

    Entries younger than twice the clone timeout are left alone so a clone in
    another process is never pulled out from under it.
    """
    try:
        entries = os.listdir(long_path(stage_parent))
    except OSError:
        return
    cutoff = time.time() - 2 * CLONE_TIMEOUT_S
    for name in entries:
        path = os.path.join(stage_parent, name)
        try:
            if os.path.getmtime(long_path(path)) < cutoff:
                _cleanup(path)
        except OSError:
            continue


def report_only(repo_url: str, target: str, reason: str) -> CloneResult:
    """No clone was attempted - tell the user precisely what to do instead."""
    if not repo_url:
        raise ValidationError("No repository URL is known for this package.")
    return CloneResult(state="skipped", repo_url=repo_url, abs_path=target,
                       error_code=None, error_message=reason,
                       manual_steps=manual_steps(repo_url, target),
                       notes=[reason])
