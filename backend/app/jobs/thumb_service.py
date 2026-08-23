"""Lazy thumbnail cache with in-flight dedupe.

WebP q82, three sizes (160/320/640), two-level fan-out on disk.

Video posters are extracted with ffmpeg when the user has it installed, via the
single sanctioned call site in ``jobs/video_frame.py``.  When ffmpeg is absent -
or the file will not decode - videos fall back to the same deterministic
placeholder as audio, 3D and model assets.  That is DECISIONS D6: frame
extraction is available when ffmpeg is, and its absence degrades gracefully.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import io
import logging
import math
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from app.jobs import video_frame

from ..config import THUMB_DIR
from ..core import db as dbmod
from ..core import imaging
from ..core.errors import NotFoundError
from ..core.fingerprint import path_hash
from ..core.pathsafe import long_path

log = logging.getLogger(__name__)

#: SECURITY_REVIEW S-05: applied at import so the budget is in force before the
#: first ``Image.open`` on a file the vault did not write.
imaging.apply_budget()

SIZES = (160, 320, 640)
QUALITY = 82

#: Bump when the *rendering* changes so previously cached thumbnails are
#: replaced.  The per-file fingerprint cannot notice this: the file is
#: identical, only our interpretation of it improved (v2 added real ffmpeg
#: poster frames for video, which used to be placeholders).  It is folded into
#: both the cache key and the ETag, so a bump invalidates the on-disk cache and
#: every browser that cached a response as `immutable`.
THUMB_VERSION = 2
PLACEHOLDER_BG = (28, 26, 24)


@dataclass(frozen=True)
class ThumbResult:
    path: str
    etag: str
    source: str          # cache | generated | placeholder
    mime: str = "image/webp"
    width: int | None = None
    height: int | None = None


def pick_size(requested: int | None) -> int:
    n = int(requested or 320)
    if n <= 160:
        return 160
    if n <= 320:
        return 320
    return 640


def versioned(fingerprint: str) -> str:
    """The cache identity: the file's fingerprint plus how we render it."""
    return f"{fingerprint}:v{THUMB_VERSION}"


def cache_path(uid: str, size: int) -> Path:
    h = hashlib.blake2b(uid.encode("utf-8"), digest_size=16).hexdigest()
    return Path(THUMB_DIR) / h[0:2] / h[2:4] / f"{h}_{size}.webp"


class ThumbService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[tuple[str, int], asyncio.Future] = {}
        from concurrent.futures import ThreadPoolExecutor

        self._ex = ThreadPoolExecutor(max_workers=min(6, os.cpu_count() or 4),
                                      thread_name_prefix="thumb")

    def shutdown(self) -> None:
        self._ex.shutdown(wait=False, cancel_futures=True)

    # -- frozen interface -------------------------------------------------
    async def get(self, uid: str, size: int) -> ThumbResult:
        size = pick_size(size)
        info = self._resolve(uid)
        if info is None:
            raise NotFoundError(f"Unknown asset '{uid}'.")
        target = cache_path(uid, size)
        stamp = versioned(info["fingerprint"])
        etag = f'"{stamp}-{size}"'
        row = self._cache_row(uid, size)
        if row and row["fingerprint"] == stamp and target.is_file():
            self._touch(uid, size)
            return ThumbResult(str(target), etag, "cache",
                               width=row["width"], height=row["height"])

        key = (uid, size)
        loop = asyncio.get_running_loop()
        with self._lock:
            fut = self._inflight.get(key)
            if fut is None:
                fut = loop.create_future()
                self._inflight[key] = fut
                owner = True
            else:
                owner = False
        if not owner:
            return await asyncio.shield(fut)

        try:
            result = await loop.run_in_executor(
                self._ex, self._generate, uid, size, info, target, etag)
            if not fut.done():
                fut.set_result(result)
            return result
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)

    #: Hard ceiling on a browser-supplied poster frame.
    MAX_RENDERED_BYTES = 4 * 1024 * 1024

    def store_rendered(self, uid: str, data_url: str) -> int:
        """Take a PNG the browser rendered for a 3D model into the cache.

        This is the one path where bytes for the cache come from the client, so
        it is deliberately strict: a `data:image/png;base64,` URL, inside a size
        cap, that Pillow can actually open as a PNG.  The image is re-encoded
        to WebP by us rather than trusted as-is, which also means a hostile
        payload never reaches disk in the form it arrived in.
        """
        from PIL import Image

        prefix = "data:image/png;base64,"
        if not data_url.startswith(prefix):
            raise ValueError("Expected a data:image/png;base64 URL.")
        try:
            raw = base64.b64decode(data_url[len(prefix):], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("The thumbnail payload is not valid base64.") from exc

        if not raw:
            raise ValueError("The thumbnail payload is empty.")
        if len(raw) > self.MAX_RENDERED_BYTES:
            raise ValueError(
                f"The thumbnail is larger than {self.MAX_RENDERED_BYTES // 1024} KB.")

        info = self._resolve(uid)
        if info is None:
            raise ValueError(f"Unknown asset '{uid}'.")
        # Only 3D models get a client-rendered poster: everything else already
        # has a server-side path, and this endpoint writes to the cache.
        if str(info.get("media") or "") != "model3d":
            raise ValueError("Rendered thumbnails are only accepted for 3D models.")

        written = 0
        for size in SIZES:
            try:
                with Image.open(io.BytesIO(raw), formats=imaging.open_formats(("PNG",))) as im:
                    if im.format != "PNG":
                        raise ValueError("The payload is not a PNG.")
                    im.load()
                    img = im.convert("RGB")
                    img.thumbnail((size, size), Image.LANCZOS)
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(f"The thumbnail could not be decoded: {exc}") from exc

            target = cache_path(uid, size)
            target.parent.mkdir(parents=True, exist_ok=True)
            buf = io.BytesIO()
            img.save(buf, format="WEBP", quality=QUALITY, method=4)
            data = buf.getvalue()
            part = target.with_suffix(".part")
            with open(part, "wb") as fh:
                fh.write(data)
            os.replace(part, target)
            written += len(data)

            now = dbmod.now_ms()
            row = (uid, size, str(target), versioned(info["fingerprint"]), len(data),
                   img.width, img.height, now, now)

            def _op(conn: sqlite3.Connection, row=row) -> None:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO thumb_cache(uid,size,cache_path,fingerprint,bytes,width,"
                    "height,generated_at,last_access_at) VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(uid,size) DO UPDATE SET cache_path=excluded.cache_path, "
                    "fingerprint=excluded.fingerprint, bytes=excluded.bytes, "
                    "width=excluded.width, height=excluded.height, "
                    "generated_at=excluded.generated_at, last_access_at=excluded.last_access_at",
                    row,
                )
                conn.commit()

            dbmod.writer().run(_op)
        log.info("stored a rendered poster for %s (%d bytes across %d sizes)",
                 uid, written, len(SIZES))
        return written

    def relocate(self, uid: str, old_path: str, new_path: str) -> None:
        """Renaming is cheap: the cache key is the uid, so only the row moves."""
        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM thumb_cache WHERE uid = ?", (uid,))
            conn.commit()

        try:
            dbmod.writer().run(_op)
        except BaseException as exc:  # noqa: BLE001
            log.debug("ignored (best effort): %s", exc)
        for size in SIZES:
            p = cache_path(uid, size)
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                continue

    def gc(self, max_mb: int | None = None) -> dict:
        conn = dbmod.get_ro()
        rows = dbmod.rows(
            conn, "SELECT uid, size, cache_path, bytes, last_access_at FROM thumb_cache "
            "ORDER BY last_access_at ASC")
        total = sum(int(r["bytes"] or 0) for r in rows)
        budget = int(max_mb or 2048) * 1024 * 1024
        removed = 0
        freed = 0
        victims: list[tuple[str, int]] = []
        for r in rows:
            path = str(r["cache_path"])
            missing = not os.path.isfile(long_path(path))
            if missing or total - freed > budget:
                if not missing:
                    with contextlib.suppress(OSError):
                        os.remove(long_path(path))
                freed += int(r["bytes"] or 0)
                removed += 1
                victims.append((str(r["uid"]), int(r["size"])))
        if victims:
            def _op(conn: sqlite3.Connection) -> None:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany("DELETE FROM thumb_cache WHERE uid=? AND size=?", victims)
                conn.commit()

            try:
                dbmod.writer().run(_op)
            except BaseException as exc:  # noqa: BLE001
                log.debug("ignored (best effort): %s", exc)
        return {"removed": removed, "freed_bytes": freed, "total_bytes": total,
                "remaining_bytes": max(0, total - freed)}

    def stats(self) -> dict:
        conn = dbmod.get_ro()
        row = dbmod.one(conn, "SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b FROM thumb_cache")
        return {"count": int(row["n"]) if row else 0,
                "bytes": int(row["b"]) if row else 0,
                "dir": str(THUMB_DIR)}

    # -- internals --------------------------------------------------------
    def _resolve(self, uid: str) -> dict | None:
        kind, _sep, num = str(uid).partition(":")
        try:
            row_id = int(num)
        except (TypeError, ValueError):
            return None
        conn = dbmod.get_ro()
        if kind == "output":
            r = dbmod.one(conn, "SELECT abs_path, fingerprint, media_kind, ext, width, "
                                "height, duration_ms FROM outputs WHERE id = ?", (row_id,))
            if r is None:
                return None
            return {"kind": "output", "path": str(r["abs_path"]),
                    "fingerprint": str(r["fingerprint"]), "media": str(r["media_kind"]),
                    "ext": str(r["ext"]), "label": None,
                    "seed": str(r["abs_path"])}
        if kind == "model":
            r = dbmod.one(conn, "SELECT m.name, m.base_model_family, m.model_role, "
                                "f.preview_path, f.fingerprint, f.abs_path FROM models m "
                                "LEFT JOIN model_files f ON f.id = m.primary_file_id "
                                "WHERE m.id = ?", (row_id,))
            if r is None:
                return None
            return {"kind": "model", "path": r["preview_path"],
                    "fingerprint": str(r["fingerprint"] or r["name"]),
                    "media": "image" if r["preview_path"] else "placeholder",
                    "ext": ".png", "label": str(r["base_model_family"] or "?"),
                    "sub": str(r["model_role"] or ""), "seed": str(r["name"])}
        if kind == "workflow":
            r = dbmod.one(conn, "SELECT name, preview_path, fingerprint, node_count, "
                                "abs_path FROM workflows WHERE id = ?", (row_id,))
            if r is None:
                return None
            preview = r["preview_path"]
            if not preview:
                sibling = os.path.splitext(str(r["abs_path"]))[0] + ".png"
                preview = sibling if os.path.isfile(long_path(sibling)) else None
            return {"kind": "workflow", "path": preview,
                    "fingerprint": str(r["fingerprint"]),
                    "media": "image" if preview else "placeholder", "ext": ".png",
                    "label": "WORKFLOW", "sub": f"{r['node_count']} nodes",
                    "seed": str(r["name"])}
        if kind == "node_package":
            r = dbmod.one(conn, "SELECT display_name, fingerprint, class_count "
                                "FROM node_packages WHERE id = ?", (row_id,))
            if r is None:
                return None
            return {"kind": "node_package", "path": None,
                    "fingerprint": str(r["fingerprint"]), "media": "placeholder",
                    "ext": ".png", "label": "NODES",
                    "sub": f"{r['class_count']} classes",
                    "seed": str(r["display_name"])}
        return None

    def _cache_row(self, uid: str, size: int):
        try:
            conn = dbmod.get_ro()
            return dbmod.one(
                conn, "SELECT fingerprint, bytes, width, height FROM thumb_cache "
                "WHERE uid = ? AND size = ?", (uid, size))
        except sqlite3.DatabaseError:
            return None

    def _touch(self, uid: str, size: int) -> None:
        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE thumb_cache SET last_access_at=? WHERE uid=? AND size=?",
                         (dbmod.now_ms(), uid, size))
            conn.commit()

        try:
            dbmod.writer().submit(_op)
        except BaseException as exc:  # noqa: BLE001
            log.debug("ignored (best effort): %s", exc)

    def _generate(self, uid: str, size: int, info: dict, target: Path,
                  etag: str) -> ThumbResult:
        try:
            from PIL import Image, ImageDraw, ImageFile
        except ImportError as exc:  # pragma: no cover
            raise NotFoundError("Pillow is unavailable; thumbnails cannot be made.") from exc
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        target.parent.mkdir(parents=True, exist_ok=True)
        source = "generated"
        img = None
        src_path = info.get("path")
        if src_path and info.get("media") == "image":
            try:
                with Image.open(long_path(str(src_path)),
                                formats=imaging.open_formats()) as im:
                    if imaging.exceeds_budget(im.size):
                        raise ValueError(
                            f"{im.size[0]}x{im.size[1]} is over the "
                            f"{imaging.MAX_IMAGE_PIXELS} pixel budget")
                    if im.format == "JPEG":
                        im.draft("RGB", (size * 2, size * 2))
                    im.load()
                    if getattr(im, "is_animated", False):
                        im.seek(0)
                    img = im.convert("RGB")
                    img.thumbnail((size, size), Image.LANCZOS)
            except Exception as exc:  # noqa: BLE001 - fall back to a placeholder
                log.debug("thumbnail decode failed for %s: %s", src_path, exc)
                img = None
        if img is None and src_path and info.get("media") == "video":
            frame = video_frame.extract_frame(str(src_path), size * 2)
            if frame:
                try:
                    with Image.open(io.BytesIO(frame),
                                    formats=imaging.open_formats(imaging.FRAME_FORMATS)) as im:
                        im.load()
                        img = im.convert("RGB")
                        img.thumbnail((size, size), Image.LANCZOS)
                    source = "generated"
                except Exception as exc:  # noqa: BLE001 - fall back to a placeholder
                    log.debug("video frame decode failed for %s: %s", src_path, exc)
                    img = None
        if img is None:
            img = self._placeholder(uid, size, info, Image, ImageDraw)
            source = "placeholder"

        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=QUALITY, method=4)
        data = buf.getvalue()
        part = target.with_suffix(".part")
        try:
            with open(part, "wb") as fh:
                fh.write(data)
            os.replace(part, target)
        except OSError as exc:
            raise NotFoundError(f"Could not write the thumbnail cache: {exc}") from exc

        # A 3D placeholder must NOT claim the cache slot.  The real poster for
        # a model is rendered by the browser and handed back later
        # (`store_rendered`), and if the gradient were recorded as cached, the
        # next GET would serve it forever -- worse, a GET that lands after the
        # poster was stored would overwrite it.  Leaving the row out means the
        # gradient is regenerated cheaply on each request until a poster
        # arrives, and the moment one does it wins.
        transient = source == "placeholder" and info.get("media") == "model3d"
        if transient:
            return ThumbResult(str(target), etag, source,
                               width=img.width, height=img.height)

        now = dbmod.now_ms()
        row = (uid, size, str(target), versioned(info["fingerprint"]), len(data),
               img.width, img.height, now, now)

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO thumb_cache(uid,size,cache_path,fingerprint,bytes,width,"
                "height,generated_at,last_access_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(uid,size) DO UPDATE SET cache_path=excluded.cache_path, "
                "fingerprint=excluded.fingerprint, bytes=excluded.bytes, "
                "width=excluded.width, height=excluded.height, "
                "generated_at=excluded.generated_at, last_access_at=excluded.last_access_at",
                row,
            )
            conn.commit()

        try:
            dbmod.writer().run(_op)
        except BaseException:  # noqa: BLE001 - the file is on disk either way
            log.debug("could not record thumb_cache row for %s", uid)
        return ThumbResult(str(target), etag, source, width=img.width, height=img.height)

    def _placeholder(self, uid: str, size: int, info: dict, Image, ImageDraw):
        """Deterministic 2-stop gradient; hue derived from the asset identity."""
        seed = str(info.get("seed") or uid)
        h = int(path_hash(seed)[:6], 16)
        hue = h % 360
        img = Image.new("RGB", (size, size), PLACEHOLDER_BG)
        draw = ImageDraw.Draw(img)
        top = _hsv(hue / 360.0, 0.42, 0.34)
        bottom = _hsv(((hue + 28) % 360) / 360.0, 0.30, 0.16)
        for y in range(size):
            t = y / max(1, size - 1)
            draw.line(
                [(0, y), (size, y)],
                fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
            )
        label = str(info.get("label") or info.get("media") or "").upper()[:12]
        sub = str(info.get("sub") or "")[:22]
        if label:
            draw.text((10, size // 2 - 14), label, fill=(238, 232, 222))
        if sub and size >= 160:
            draw.text((10, size // 2 + 2), sub, fill=(190, 182, 172))
        media = str(info.get("media") or "")
        glyph = {"video": "▶", "audio": "♪", "model3d": "◈",
                 "text": "≡"}.get(media)
        if glyph:
            draw.text((size - 26, size - 26), glyph, fill=(238, 232, 222))
        return img


def _hsv(h: float, s: float, v: float) -> tuple[int, int, int]:
    i = int(h * 6.0)
    f = h * 6.0 - i
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    r, g, b = [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i % 6]
    return int(r * 255), int(g * 255), int(b * 255)


_service: ThumbService | None = None
_lock = threading.Lock()


def get_thumb_service() -> ThumbService:
    global _service
    with _lock:
        if _service is None:
            _service = ThumbService()
        return _service


def shutdown_thumb_service() -> None:
    global _service
    with _lock:
        if _service is not None:
            _service.shutdown()
            _service = None


def _unused(_: math) -> None:  # pragma: no cover
    return None
