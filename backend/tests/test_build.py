"""Build, lint and branding gates — the checks that stop a broken handover.

BUILD_PLAN 8 makes ``ruff check backend``, ``npm run build`` and ``pytest`` the
acceptance criteria for this wave, so they belong in the suite rather than in a
runbook nobody executes.  ``ruff`` is cheap and always runs; the frontend build
needs ``node_modules`` and takes seconds, so it is marked ``slow``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from c6 import AUTHOR, AUTHOR_FULL, attribution_hits, bare_token_hits

BUNDLE_BUDGET_KB = 400  # ARCHITECTURE 10 targets 350; 344.08 kB measured


def run(cmd, cwd, timeout=900):
    return subprocess.run(  # noqa: S603
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False)


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

def test_ruff_is_clean(repo_root):
    """Cross-agent rule 5: every agent leaves its area lint-clean before handoff."""
    python = repo_root / "venv" / "Scripts" / "python.exe"
    if not python.exists():
        pytest.skip("no venv")
    r = run([str(python), "-m", "ruff", "check", "backend"], repo_root, timeout=300)
    assert r.returncode == 0, f"ruff check backend failed:\n{r.stdout}\n{r.stderr}"


COUNT_ROUTES = (
    "from app.main import app\n"
    "seen = 0\n"
    "stack = list(app.routes)\n"
    "while stack:\n"
    "    r = stack.pop()\n"
    "    inner = getattr(r, 'original_router', None)\n"
    "    if inner is not None:\n"
    "        stack.extend(inner.routes)\n"
    "    elif getattr(r, 'path', None):\n"
    "        seen += 1\n"
    "print('ROUTES', seen)\n"
)


def test_the_backend_imports_cleanly(repo_root):
    """Importing the app must register the whole v1 surface, not a stub.

    This FastAPI version keeps an included router as a wrapper object rather than
    flattening its routes into ``app.routes``, so a naive ``len(app.routes)``
    reads 8 and would pass just as happily for an app with no endpoints at all.
    """
    python = repo_root / "venv" / "Scripts" / "python.exe"
    if not python.exists():
        pytest.skip("no venv")
    r = run([str(python), "-c", COUNT_ROUTES], repo_root / "backend", timeout=180)
    assert r.returncode == 0, f"importing app.main failed:\n{r.stderr}"
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("ROUTES")]
    assert line, f"no route count printed:\n{r.stdout}\n{r.stderr}"
    count = int(line[-1].split()[1])
    assert count > 90, f"only {count} routes registered; the v1 surface is incomplete"


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_the_frontend_builds(repo_root):
    """``npm run build`` must pass, and the bundle must stay inside its budget."""
    frontend = repo_root / "frontend"
    if not (frontend / "node_modules").is_dir():
        pytest.skip("frontend/node_modules is not installed")
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        pytest.skip("npm is not on PATH")
    r = run([npm, "run", "build"], frontend)
    assert r.returncode == 0, (
        f"npm run build failed:\n{r.stdout[-4000:]}\n{r.stderr[-4000:]}")

    dist = frontend / "dist"
    assert (dist / "index.html").exists(), "the build produced no index.html"
    js = sorted((dist / "assets").glob("*.js"),
                key=lambda p: p.stat().st_size, reverse=True)
    assert js, "the build produced no JavaScript bundle"
    kb = js[0].stat().st_size / 1024
    print(f"\n  [budget] frontend bundle {kb:.2f} kB "
          f"(budget {BUNDLE_BUDGET_KB} kB, baseline 344.08 kB)")
    assert kb <= BUNDLE_BUDGET_KB, f"main bundle is {kb:.1f} kB"


def test_no_typescript_was_introduced(repo_root):
    """Standing constraint: the stack is fixed — no TypeScript, no ORM, no Electron."""
    src = repo_root / "frontend" / "src"
    if not src.is_dir():
        pytest.skip("no frontend/src")
    ts = [p for p in src.rglob("*") if p.suffix in (".ts", ".tsx")]
    assert not ts, f"TypeScript files present: {[str(p) for p in ts]}"


def test_the_frontend_proxies_to_the_agreed_port(repo_root):
    """D1: 8000 was retired."""
    cfg = repo_root / "frontend" / "vite.config.js"
    if not cfg.exists():
        pytest.skip("no vite.config.js")
    text = cfg.read_text(encoding="utf-8")
    assert "8127" in text, "the vite proxy does not point at port 8127"
    assert "8000" not in text, "vite.config.js still references the retired port 8000"


# ---------------------------------------------------------------------------
# C6 — authorship
# ---------------------------------------------------------------------------

SCANNED_SUFFIXES = (".py", ".js", ".jsx", ".css", ".md", ".bat", ".ps1", ".json",
                    ".html", ".txt", ".cfg", ".ini", ".toml", ".yaml", ".yml")
SKIP_DIRS = {"venv", "node_modules", ".git", "__pycache__", "dist", ".ruff_cache",
             ".pytest_cache", "data", ".vite"}
# ``c6.py`` is the one file that has to spell the vocabulary out.
SKIP_FILES = {"c6.py"}


def product_files(repo_root):
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(SCANNED_SUFFIXES) and name not in SKIP_FILES:
                yield os.path.join(dirpath, name)


def read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError:
        return ""


def test_no_ai_tool_attribution_anywhere_in_the_product(repo_root):
    """C6 across every product file — never third-party dependency source."""
    offenders = []
    for path in product_files(repo_root):
        hits = attribution_hits(read(path))
        offenders.extend(f"{os.path.relpath(path, repo_root)}: {h!r}" for h in hits)
    assert not offenders, "C6 attribution violations:\n" + "\n".join(offenders[:40])


BARE_TOKEN_FILES = (
    "start_app.bat", "start_app.ps1", "stop_app.bat",
    "install_dependencies.bat", "install_dependencies.ps1",
    "README.md", "package.json", "vite.config.js", "index.html",
)


def test_user_facing_files_carry_no_vendor_name_at_all(repo_root):
    """Launchers, manifests and the README are held to the stricter bar.

    None of them has any reason to name a vendor, so any mention is attribution.
    """
    offenders = []
    for rel in BARE_TOKEN_FILES:
        for path in (repo_root / rel, repo_root / "frontend" / rel):
            if not path.is_file():
                continue
            hits = bare_token_hits(read(str(path)))
            offenders.extend(f"{path.relative_to(repo_root)}: {h!r}" for h in hits)
    assert not offenders, "C6 violations in user-facing files:\n" + "\n".join(offenders)


def test_ui_strings_carry_no_vendor_name(repo_root):
    """C6 explicitly covers UI strings."""
    src = repo_root / "frontend" / "src"
    if not src.is_dir():
        pytest.skip("no frontend/src")
    offenders = []
    for path in src.rglob("*"):
        if path.suffix not in (".js", ".jsx", ".css", ".html"):
            continue
        hits = bare_token_hits(path.read_text(encoding="utf-8", errors="ignore"))
        offenders.extend(f"{path.relative_to(repo_root)}: {h!r}" for h in hits)
    assert not offenders, "C6 violations in UI source:\n" + "\n".join(offenders)


def test_package_json_names_no_ai_vendor(repo_root):
    pkg = repo_root / "frontend" / "package.json"
    if not pkg.exists():
        pytest.skip("no package.json")
    blob = json.dumps(json.loads(pkg.read_text(encoding="utf-8")))
    assert not bare_token_hits(blob), f"C6 violation in package.json: {bare_token_hits(blob)}"


def test_mcp_client_names_are_generic(repo_root):
    """C6: an MCP client is a 'desktop MCP client', never a vendor brand."""
    offenders = []
    for path in (repo_root / "backend" / "app" / "mcp").rglob("*.py"):
        hits = bare_token_hits(read(str(path)))
        offenders.extend(f"{path.relative_to(repo_root)}: {h!r}" for h in hits)
    assert not offenders, "C6 violations in the MCP server:\n" + "\n".join(offenders)


def test_the_product_is_attributed_to_its_author(repo_root):
    """The positive half of C6 — the branding must actually be present."""
    readme = repo_root / "README.md"
    if not readme.exists():
        pytest.skip("no README.md")
    text = readme.read_text(encoding="utf-8", errors="ignore")
    assert AUTHOR in text, f"README does not name {AUTHOR}"
    assert AUTHOR_FULL in text, f"README does not name {AUTHOR_FULL}"
