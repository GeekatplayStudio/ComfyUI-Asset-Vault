"""S6 - registry enrichment plus package metadata harvesting.

Reads ComfyUI-Manager's ``extension-node-map.json`` (keyed by git remote URL),
``pyproject.toml``, ``requirements.txt``, README, LICENSE, and ``.git`` **as
files** - never ``subprocess``, never ``git fetch`` on the scan path.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..core.pathsafe import long_path

REGISTRY_RELPATHS = (
    os.path.join("custom_nodes", "ComfyUI-Manager", "extension-node-map.json"),
    os.path.join("custom_nodes", "ComfyUI-Manager", "node_db", "new",
                 "extension-node-map.json"),
    os.path.join("user", "default", "ComfyUI-Manager", "cache",
                 "extension-node-map.json"),
)

_GIT_URL_RE = re.compile(r"url\s*=\s*(.+)")
_SECTION_RE = re.compile(r'^\s*\[(?:remote\s+"([^"]+)"|([^\]]+))\]\s*$')


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def normalize_repo_url(url: str | None) -> str | None:
    if not url:
        return None
    u = url.strip()
    u = re.sub(r"^git\+", "", u)
    u = re.sub(r"^ssh://", "https://", u)
    u = re.sub(r"^git@([^:]+):", r"https://\1/", u)
    u = re.sub(r"^https?://[^@/]+@", "https://", u)  # strip credentials
    u = re.sub(r"\.git/?$", "", u)
    u = u.rstrip("/")
    m = re.match(r"^(https?)://([^/]+)/(.*)$", u)
    if m:
        u = f"https://{m.group(2).lower()}/{m.group(3)}"
    return u or None


def repo_basename(url: str | None) -> str | None:
    n = normalize_repo_url(url)
    return n.rsplit("/", 1)[-1] if n else None


class Registry:
    """ComfyUI-Manager's extension-node-map, loaded lazily and cached."""

    def __init__(self) -> None:
        self.by_url: dict[str, tuple[list[str], dict]] = {}
        self.by_folder: dict[str, tuple[list[str], dict]] = {}
        self.loaded = False
        self.source: str | None = None

    def load(self, comfy_root: Path | None) -> Registry:
        if self.loaded or comfy_root is None:
            return self
        self.loaded = True
        for rel in REGISTRY_RELPATHS:
            p = comfy_root / rel
            try:
                if not p.is_file():
                    continue
                with open(long_path(p), "rb") as fh:
                    data = json.loads(fh.read().decode("utf-8", "replace"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            self.source = str(p)
            for url, payload in data.items():
                if not isinstance(payload, list) or len(payload) < 1:
                    continue
                node_ids = [str(x) for x in payload[0]] if isinstance(payload[0], list) else []
                meta = payload[1] if len(payload) > 1 and isinstance(payload[1], dict) else {}
                key = normalize_repo_url(str(url))
                if key:
                    self.by_url[key.lower()] = (node_ids, meta)
                title = meta.get("title_aux") or meta.get("title")
                base = repo_basename(str(url))
                for name in filter(None, (title, base)):
                    self.by_folder.setdefault(str(name).lower(), (node_ids, meta))
            break
        return self

    def lookup(self, repo_url: str | None, folder_name: str | None):
        key = normalize_repo_url(repo_url)
        if key:
            hit = self.by_url.get(key.lower())
            if hit:
                return hit, "url"
        if folder_name:
            hit = self.by_folder.get(folder_name.lower())
            if hit:
                return hit, "folder"
            hit = self.by_folder.get(folder_name.lower().replace("comfyui-", ""))
            if hit:
                return hit, "folder"
        return None, None

    def package_for_node(self, node_id: str) -> dict | None:
        """Reverse lookup: which package registers this class id?"""
        if not self._reverse:
            for url, (ids, meta) in self.by_url.items():
                for nid in ids:
                    self._reverse.setdefault(nid, (url, meta))
        hit = self._reverse.get(node_id)
        if not hit:
            return None
        url, meta = hit
        return {"repo_url": url, "package": meta.get("title_aux") or repo_basename(url)}

    _reverse: dict[str, tuple[str, dict]] = {}


_registry = Registry()
_registry_root: str | None = None


def get_registry(comfy_root: Path | None = None) -> Registry:
    """Cached per ComfyUI root.

    The cache must be keyed on the root: a singleton that only tracks "loaded"
    keeps an empty registry forever once it has been asked about a root with no
    ComfyUI-Manager, which silently disables S6 enrichment after the user
    changes the ComfyUI path.
    """
    global _registry, _registry_root
    key = os.path.normcase(str(comfy_root)) if comfy_root is not None else None
    if _registry.loaded and key != _registry_root:
        _registry = Registry()
    _registry_root = key
    return _registry.load(comfy_root)


def reset_registry() -> None:
    global _registry, _registry_root
    _registry = Registry()
    _registry_root = None


# ---------------------------------------------------------------------------
# Package metadata
# ---------------------------------------------------------------------------

@dataclass
class PackageMeta:
    display_name: str | None = None
    author: str | None = None
    publisher_id: str | None = None
    registry_id: str | None = None
    description: str | None = None
    long_description: str | None = None
    icon_url: str | None = None
    homepage_url: str | None = None
    license: str | None = None
    version: str | None = None
    python_deps: list[str] = field(default_factory=list)
    has_web_directory: bool = False
    repo_url: str | None = None
    git_branch: str | None = None
    git_commit: str | None = None
    git_commit_at: int | None = None
    last_fetch_at: int | None = None


def _read_text(path: Path, limit: int = 200_000) -> str | None:
    try:
        with open(long_path(path), "rb") as fh:
            raw = fh.read(limit)
    except OSError:
        return None
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def read_pyproject(pkg_dir: Path, meta: PackageMeta) -> None:
    p = pkg_dir / "pyproject.toml"
    try:
        if not p.is_file():
            return
        with open(long_path(p), "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    if isinstance(project, dict):
        meta.registry_id = project.get("name") or meta.registry_id
        meta.description = meta.description or (project.get("description") or None)
        meta.version = project.get("version") or meta.version
        lic = project.get("license")
        if isinstance(lic, dict):
            meta.license = lic.get("text") or lic.get("file") or meta.license
        elif isinstance(lic, str):
            meta.license = lic
        deps = project.get("dependencies")
        if isinstance(deps, list):
            meta.python_deps = [str(d) for d in deps][:200]
        urls = project.get("urls")
        if isinstance(urls, dict):
            meta.homepage_url = (urls.get("Repository") or urls.get("Homepage")
                                 or urls.get("repository") or meta.homepage_url)
        authors = project.get("authors")
        if isinstance(authors, list) and authors and isinstance(authors[0], dict):
            meta.author = authors[0].get("name") or meta.author
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    comfy = tool.get("comfy") if isinstance(tool, dict) else None
    if isinstance(comfy, dict):
        meta.publisher_id = comfy.get("PublisherId") or meta.publisher_id
        meta.display_name = comfy.get("DisplayName") or meta.display_name
        meta.icon_url = comfy.get("Icon") or meta.icon_url
        meta.author = meta.author or comfy.get("PublisherId")


def read_requirements(pkg_dir: Path, meta: PackageMeta) -> None:
    if meta.python_deps:
        return
    text = _read_text(pkg_dir / "requirements.txt", 64_000)
    if not text:
        return
    deps = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "-", "git+")):
            continue
        deps.append(s[:200])
    meta.python_deps = deps[:200]


def read_readme(pkg_dir: Path, meta: PackageMeta) -> None:
    for name in ("README.md", "readme.md", "README.MD", "README.rst", "README.txt", "README"):
        text = _read_text(pkg_dir / name, 100_000)
        if not text:
            continue
        lines = [ln.strip() for ln in text.splitlines()]
        paras: list[str] = []
        buf: list[str] = []
        for ln in lines:
            if ln.startswith(("#", "!", "<img", "<p", "---", "===", "[!")) or not ln:
                if buf:
                    paras.append(" ".join(buf))
                    buf = []
                continue
            buf.append(ln)
            if len(paras) >= 1 and len(" ".join(buf)) > 400:
                break
        if buf:
            paras.append(" ".join(buf))
        first = next((p for p in paras if len(p) > 30), paras[0] if paras else None)
        if first:
            meta.long_description = first[:4000]
            if not meta.description:
                meta.description = first[:400]
        return


def read_license(pkg_dir: Path, meta: PackageMeta) -> None:
    if meta.license:
        return
    for name in ("LICENSE", "LICENSE.txt", "LICENSE.md", "LICENCE", "COPYING"):
        text = _read_text(pkg_dir / name, 8000)
        if not text:
            continue
        head = text[:2000].lower()
        for needle, label in (("mit license", "MIT"), ("apache license", "Apache-2.0"),
                              ("gnu general public license", "GPL"),
                              ("gnu affero", "AGPL"), ("bsd ", "BSD"),
                              ("mozilla public license", "MPL-2.0"),
                              ("creative commons", "CC")):
            if needle in head:
                meta.license = label
                return
        meta.license = "Custom"
        return


def read_git(pkg_dir: Path, meta: PackageMeta) -> None:
    """Read ``.git`` as plain files - no subprocess on the scan hot path."""
    gitdir = pkg_dir / ".git"
    try:
        if gitdir.is_file():
            text = _read_text(gitdir, 4096) or ""
            m = re.search(r"gitdir:\s*(.+)", text)
            if m:
                cand = Path(m.group(1).strip())
                gitdir = cand if cand.is_absolute() else (pkg_dir / cand)
        if not gitdir.is_dir():
            return
    except OSError:
        return

    cfg = _read_text(gitdir / "config", 64_000)
    if cfg:
        section = None
        for line in cfg.splitlines():
            m = _SECTION_RE.match(line)
            if m:
                section = m.group(1) or (m.group(2) or "").strip()
                continue
            if section == "origin":
                mu = _GIT_URL_RE.match(line.strip())
                if mu:
                    meta.repo_url = mu.group(1).strip()
                    break

    head = _read_text(gitdir / "HEAD", 4096)
    ref = None
    if head:
        head = head.strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            meta.git_branch = ref.rsplit("/", 1)[-1]
        elif re.fullmatch(r"[0-9a-f]{40}", head):
            meta.git_commit = head
    if ref and not meta.git_commit:
        sha = _read_text(gitdir / Path(ref.replace("/", os.sep)), 128)
        if sha and re.fullmatch(r"[0-9a-f]{40}", sha.strip()):
            meta.git_commit = sha.strip()
        else:
            packed = _read_text(gitdir / "packed-refs", 512_000) or ""
            for line in packed.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    meta.git_commit = parts[0]
                    break

    logs = _read_text(gitdir / "logs" / "HEAD", 256_000)
    if logs:
        lines = [ln for ln in logs.splitlines() if ln.strip()]
        if lines:
            m = re.search(r"\s(\d{9,11})\s[+-]\d{4}\t", lines[-1])
            if m:
                meta.git_commit_at = int(m.group(1)) * 1000
    try:
        fh = gitdir / "FETCH_HEAD"
        if fh.is_file():
            meta.last_fetch_at = int(fh.stat().st_mtime * 1000)
    except OSError:
        pass


def read_web_directory(pkg_dir: Path, meta: PackageMeta) -> None:
    for name in ("web", "js", "dist"):
        try:
            if (pkg_dir / name).is_dir():
                meta.has_web_directory = True
                return
        except OSError:
            continue


def read_node_list(pkg_dir: Path) -> list[str]:
    for name in ("node_list.json", "nodes.json"):
        text = _read_text(pkg_dir / name, 512_000)
        if not text:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict):
            return [str(k) for k in data][:2000]
        if isinstance(data, list):
            return [str(x) for x in data if isinstance(x, str)][:2000]
    return []


def collect_package_meta(pkg_dir: Path, folder_name: str) -> PackageMeta:
    meta = PackageMeta()
    read_pyproject(pkg_dir, meta)
    read_requirements(pkg_dir, meta)
    read_readme(pkg_dir, meta)
    read_license(pkg_dir, meta)
    read_git(pkg_dir, meta)
    read_web_directory(pkg_dir, meta)
    if not meta.display_name:
        meta.display_name = folder_name
    if not meta.author and meta.repo_url:
        norm = normalize_repo_url(meta.repo_url) or ""
        parts = norm.rsplit("/", 2)
        if len(parts) >= 2:
            meta.author = parts[-2]
    return meta


def repo_url_is_suspect(repo_url: str | None, folder_name: str,
                        registry: Registry | None = None) -> bool:
    """``was-ns`` points at Comfy-Org/ComfyUI - claiming updates from it is worse
    than claiming nothing."""
    base = repo_basename(repo_url)
    if not base:
        return False
    fa = re.sub(r"[^a-z0-9]", "", base.lower())
    fb = re.sub(r"[^a-z0-9]", "", folder_name.lower().replace(".disabled", ""))
    if fa == fb or fa in fb or fb in fa:
        return False
    if registry is not None:
        hit, how = registry.lookup(repo_url, folder_name)
        if hit and how == "url":
            return False
    return True
