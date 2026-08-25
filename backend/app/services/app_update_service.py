"""Self-update: check this app's own GitHub releases, and stage a new one.

The rule that shapes every function here is that **this module never replaces a
running file.**  It does exactly three things - ask GitHub what the newest
release is, download that release's archive, and unpack it into a staging
directory under ``backend/data`` - and then stops.  The swap is performed by
``apply_update.py`` at the repository root, which the launcher runs *before*
the engine starts, when nothing is imported and nothing is serving.

What that buys:

* a half-applied update cannot exist while the app is answering requests;
* the staged tree is inert until the owner restarts, so "download" and
  "install" stay separate decisions with separate consent;
* rollback is a directory move, because the previous tree is kept.

Provenance, stated honestly.  The archive is checked against the SHA-256 that
the release publishes, and the size and archive shape are bounded.  That
detects a truncated or corrupted download; it is **not** proof of authorship,
because the checksum travels from the same host as the file.  Nothing in this
module or the UI claims otherwise - a signed release would be needed for that,
and this app does not have one.

The repository is a module constant, not a setting: no configuration value can
point the updater at a different project.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .. import config as buildcfg
from ..core import config_service
from ..core import db as dbmod
from ..core.errors import FeatureUnavailable, UpstreamUnavailable, ValidationError
from ..enable import hosts

log = logging.getLogger(__name__)

#: Pinned.  Never read from config - see the module docstring.
REPO_OWNER = "GeekatplayStudio"
REPO_NAME = "ComfyUI-Asset-Vault"
RELEASES_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases"

TIMEOUT_S = 15.0
DOWNLOAD_TIMEOUT_S = 300.0
#: A release archive is a few MB of source plus a built interface.  Well under
#: this, and a file that is not is refused rather than written to disk.
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20_000
#: Refuse an archive whose uncompressed size is absurd for its download size.
MAX_EXPANSION_RATIO = 100
CHECK_TTL_MS = 6 * 60 * 60 * 1000

STAGE_ROOT = buildcfg.DATA_DIR / "updates"
STAGE_DIR = STAGE_ROOT / "staged"
DOWNLOAD_DIR = STAGE_ROOT / "download"
MARKER = STAGE_ROOT / "pending.json"

#: What the archive is allowed to place, relative to the install root.  An
#: entry outside these never lands, so a release cannot rewrite the launcher
#: into something else or drop a file into the owner's model folders.
ALLOWED_TOPLEVEL = ("backend/app", "frontend/dist", "docs")
ALLOWED_FILES = (
    "apply_update.py", "start_app.bat", "start_app.ps1", "start_app.sh",
    "stop_app.bat", "stop_app.sh", "show_service_status.ps1",
    "show_service_status.sh", "install_dependencies.bat",
    "install_dependencies.ps1", "install_dependencies.sh",
    "backend/requirements.txt", "LICENSE", "README.md",
)

_VERSION_RE = re.compile(r"^\D*(\d+(?:\.\d+)*)")


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    notes: str
    published_at: str | None
    asset_name: str | None
    asset_url: str | None
    asset_bytes: int
    asset_sha256: str | None
    html_url: str


def parse_version(text: str | None) -> tuple[int, ...]:
    """``"v2.3.10"`` -> ``(2, 3, 10)``.  Unparsable sorts lowest."""
    match = _VERSION_RE.match(str(text or "").strip())
    if not match:
        return ()
    return tuple(int(p) for p in match.group(1).split("."))


def is_newer(candidate: str | None, current: str | None) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return bool(a) and a > b


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def _require_online() -> None:
    cfg = config_service.get_config()
    if not cfg.online_enabled:
        raise FeatureUnavailable(
            "Outbound lookups are disabled, so the update check cannot run.",
            details={"reason": "online_disabled",
                     "hint": "Settings -> Search -> Allow outbound lookups at all"})


def _get(url: str, *, timeout: float, headers: dict | None = None):
    """One allowlisted GET.  Every redirect hop is re-validated by host."""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - httpx ships in requirements
        raise FeatureUnavailable("httpx is not installed.") from exc

    checked = hosts.check(url, kind=hosts.KIND_RELEASE)
    request_headers = {"User-Agent": f"GeekatplayAssetVault/{buildcfg.VERSION}",
                       **(headers or {})}
    current = checked
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for hop in range(hosts.MAX_REDIRECTS + 1):
            response = client.get(current.url, headers=request_headers)
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("location")
            nxt = hosts.check_redirect(location, current=current, hop=hop)
            request_headers = hosts.strip_auth_on_host_change(
                request_headers, current, nxt)
            current = nxt
    raise UpstreamUnavailable("The release download redirected too many times.")


def _cache_get(key: str) -> dict | None:
    try:
        row = dbmod.one(dbmod.get_ro(),
                        "SELECT body_json, expires_at FROM http_cache WHERE cache_key = ?",
                        (key,))
    except Exception:  # noqa: BLE001 - a cache miss is never an error
        return None
    if row is None or int(row["expires_at"] or 0) < dbmod.now_ms():
        return None
    try:
        return json.loads(row["body_json"]) if row["body_json"] else None
    except (TypeError, ValueError):
        return None


def _cache_put(key: str, body: dict, ttl_ms: int) -> None:
    now = dbmod.now_ms()

    def _op(conn) -> None:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO http_cache(cache_key,provider,status,body_json,fetched_at,"
            "expires_at,error) VALUES (?,?,?,?,?,?,NULL) "
            "ON CONFLICT(cache_key) DO UPDATE SET body_json=excluded.body_json, "
            "fetched_at=excluded.fetched_at, expires_at=excluded.expires_at",
            (key, "github", 200, json.dumps(body, ensure_ascii=False, default=str),
             now, now + ttl_ms))
        conn.commit()

    try:
        dbmod.writer().submit(_op)
    except Exception as exc:  # noqa: BLE001 - caching is best effort
        log.debug("update-check cache write skipped: %s", exc)


def _asset_of(payload: dict) -> tuple[dict | None, str | None]:
    """The release archive, and the SHA-256 published beside it if there is one."""
    assets = payload.get("assets") if isinstance(payload.get("assets"), list) else []
    archive = next((a for a in assets if isinstance(a, dict)
                    and str(a.get("name", "")).lower().endswith(".zip")), None)
    if archive is None:
        return None, None
    # GitHub exposes a per-asset digest ("sha256:<hex>") on newer releases.
    digest = str(archive.get("digest") or "")
    if digest.lower().startswith("sha256:") and len(digest) == 71:
        return archive, digest.split(":", 1)[1].lower()
    return archive, None


def _to_release(payload: dict) -> Release:
    tag = str(payload.get("tag_name") or "")
    asset, sha256 = _asset_of(payload)
    return Release(
        version=tag.lstrip("vV") or tag,
        tag=tag,
        notes=str(payload.get("body") or "").strip(),
        published_at=payload.get("published_at"),
        asset_name=str(asset.get("name")) if asset else None,
        asset_url=str(asset.get("browser_download_url")) if asset else None,
        asset_bytes=int(asset.get("size") or 0) if asset else 0,
        asset_sha256=sha256,
        html_url=str(payload.get("html_url") or RELEASES_PAGE),
    )


def fetch_latest(*, force: bool = False) -> Release | None:
    """The newest published release, or ``None`` when there is not one."""
    _require_online()
    key = f"github:release:{REPO_OWNER}/{REPO_NAME}"
    payload = None if force else _cache_get(key)
    if payload is None:
        response = _get(RELEASES_URL, timeout=TIMEOUT_S,
                        headers={"Accept": "application/vnd.github+json"})
        if response.status_code == 404:
            return None
        if response.status_code == 403:
            raise UpstreamUnavailable(
                "GitHub rate-limited the update check. Try again later.",
                details={"status": 403})
        if response.status_code != 200:
            raise UpstreamUnavailable(
                f"GitHub answered {response.status_code} for the release check.",
                details={"status": response.status_code})
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamUnavailable("GitHub returned an unreadable release.") from exc
        _cache_put(key, payload, CHECK_TTL_MS)
    if payload.get("draft") or payload.get("prerelease"):
        return None
    return _to_release(payload)


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_members(archive: zipfile.ZipFile, downloaded: int) -> list[tuple[str, str]]:
    """Validate every entry and map it to its install-relative destination.

    Refuses absolute paths, traversal, links, an entry count or expansion ratio
    that says "zip bomb", and anything outside :data:`ALLOWED_TOPLEVEL` /
    :data:`ALLOWED_FILES`.  Returns ``(member_name, relative_destination)``.
    """
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise ValidationError(
            f"The release archive declares {len(infos)} entries; the limit is "
            f"{MAX_ARCHIVE_ENTRIES}.")
    total = sum(int(i.file_size or 0) for i in infos)
    if downloaded and total > downloaded * MAX_EXPANSION_RATIO:
        raise ValidationError(
            "The release archive expands far beyond its download size and was refused.")

    # Releases are packaged with a single top-level folder; strip it.
    roots = {name.split("/", 1)[0] for name in archive.namelist() if name.strip()}
    prefix = f"{next(iter(roots))}/" if len(roots) == 1 else ""

    out: list[tuple[str, str]] = []
    for info in infos:
        name = info.filename
        if info.is_dir():
            continue
        # Symlinks and devices are stored with their type in the high bits.
        if (info.external_attr >> 28) not in (0, 0o10):
            raise ValidationError(f"The release archive contains a link entry: {name}")
        rel = name[len(prefix):] if prefix and name.startswith(prefix) else name
        rel = rel.replace("\\", "/").lstrip("/")
        if not rel or rel.startswith("../") or "/../" in rel or ":" in rel:
            raise ValidationError(f"The release archive contains an unsafe path: {name}")
        if rel in ALLOWED_FILES or any(
                rel.startswith(top + "/") for top in ALLOWED_TOPLEVEL):
            out.append((name, rel))
    if not out:
        raise ValidationError("The release archive held nothing this app may install.")
    return out


def _reset(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def stage(release: Release) -> dict:
    """Download, verify and unpack one release.  Never touches the live tree."""
    _require_online()
    if not release.asset_url:
        raise ValidationError(
            "That release publishes no downloadable archive. Install it by hand "
            "from the releases page.",
            details={"releases_url": release.html_url})
    if release.asset_bytes and release.asset_bytes > MAX_ARCHIVE_BYTES:
        raise ValidationError(
            f"The release archive is {release.asset_bytes} bytes, over the "
            f"{MAX_ARCHIVE_BYTES}-byte ceiling.")

    _reset(DOWNLOAD_DIR)
    archive_path = DOWNLOAD_DIR / "release.zip"
    response = _get(release.asset_url, timeout=DOWNLOAD_TIMEOUT_S)
    if response.status_code != 200:
        raise UpstreamUnavailable(
            f"The release download answered {response.status_code}.",
            details={"status": response.status_code})
    body = response.content
    if len(body) > MAX_ARCHIVE_BYTES:
        raise ValidationError("The release download exceeded its size ceiling.")
    archive_path.write_bytes(body)

    actual = _sha256_file(archive_path)
    if release.asset_sha256 and actual != release.asset_sha256:
        _reset(DOWNLOAD_DIR)
        raise ValidationError(
            "The downloaded archive does not match the checksum published with "
            "the release; it was discarded.",
            details={"expected": release.asset_sha256, "actual": actual})

    _reset(STAGE_DIR)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = _safe_members(archive, len(body))
            for name, rel in members:
                target = STAGE_DIR / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
    except zipfile.BadZipFile as exc:
        _reset(STAGE_DIR)
        _reset(DOWNLOAD_DIR)
        raise ValidationError("The release archive could not be read.") from exc

    marker = {
        "version": release.version,
        "tag": release.tag,
        "from_version": buildcfg.VERSION,
        "sha256": actual,
        "verified": bool(release.asset_sha256),
        "staged_at": dbmod.now_ms(),
        "files": len(members),
        "notes": release.notes[:20_000],
        "html_url": release.html_url,
    }
    STAGE_ROOT.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(json.dumps(marker, indent=2), encoding="utf-8")
    # The archive itself is no longer needed once it is unpacked and recorded.
    _reset(DOWNLOAD_DIR)
    log.info("staged update %s -> %s (%d files)", buildcfg.VERSION,
             release.version, len(members))
    return marker


def pending() -> dict | None:
    """The staged update waiting for a restart, if there is one."""
    if not MARKER.is_file():
        return None
    try:
        data = json.loads(MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not STAGE_DIR.is_dir():
        return None
    return data if isinstance(data, dict) else None


def discard() -> bool:
    """Throw a staged update away.  The running install is untouched."""
    had = pending() is not None
    shutil.rmtree(STAGE_DIR, ignore_errors=True)
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    MARKER.unlink(missing_ok=True)
    return had


# ---------------------------------------------------------------------------
# The status the UI renders
# ---------------------------------------------------------------------------

def status(*, force: bool = False) -> dict:
    """Current version, newest release, and whatever is staged.  Never raises."""
    cfg = config_service.get_config()
    staged = pending()
    out = {
        "current_version": buildcfg.VERSION,
        "latest_version": None,
        "has_update": False,
        "state": "unknown",
        "reason": None,
        "notes": None,
        "published_at": None,
        "download_bytes": 0,
        "checksum_published": False,
        "releases_url": RELEASES_PAGE,
        "repository": f"{REPO_OWNER}/{REPO_NAME}",
        "check_enabled": cfg.app_update_check_enabled,
        "auto_download": cfg.app_update_auto_download,
        "online_enabled": cfg.online_enabled,
        "last_check": int(cfg.raw.get("app_update_last_check") or 0),
        "skipped_version": cfg.raw.get("app_update_skipped_version"),
        "pending": staged,
    }
    if not cfg.app_update_check_enabled:
        out["state"] = "disabled"
        out["reason"] = "Update checks are turned off."
        return out
    if not cfg.online_enabled:
        out["state"] = "offline"
        out["reason"] = ("Outbound lookups are disabled, so the vault cannot ask "
                         "GitHub what the newest release is.")
        return out
    try:
        release = fetch_latest(force=force)
    except (FeatureUnavailable, UpstreamUnavailable, ValidationError) as exc:
        out["state"] = "error"
        out["reason"] = getattr(exc, "message", str(exc))
        return out
    except Exception as exc:  # noqa: BLE001 - a check must never break Settings
        log.debug("update check failed: %s", exc)
        out["state"] = "error"
        out["reason"] = "The update check could not be completed."
        return out

    config_service.set_config({"app_update_last_check": dbmod.now_ms()})
    if release is None:
        out["state"] = "current"
        out["reason"] = "No published release was found."
        return out
    out.update({
        "latest_version": release.version,
        "notes": release.notes,
        "published_at": release.published_at,
        "download_bytes": release.asset_bytes,
        "checksum_published": bool(release.asset_sha256),
        "releases_url": release.html_url,
        "downloadable": bool(release.asset_url),
    })
    out["has_update"] = is_newer(release.version, buildcfg.VERSION)
    out["state"] = "update_available" if out["has_update"] else "current"
    return out


def check_and_maybe_download() -> dict:
    """The startup / Settings path: check, and download only if asked to."""
    result = status()
    cfg = config_service.get_config()
    if not (result.get("has_update") and cfg.app_update_auto_download):
        return result
    if result.get("skipped_version") == result.get("latest_version"):
        return result
    if (result.get("pending") or {}).get("version") == result.get("latest_version"):
        return result
    try:
        release = fetch_latest()
        if release is not None:
            result["pending"] = stage(release)
    except Exception as exc:  # noqa: BLE001 - a failed auto-download is informational
        log.info("automatic update download skipped: %s", exc)
        result["reason"] = "The update could not be downloaded automatically."
    return result
