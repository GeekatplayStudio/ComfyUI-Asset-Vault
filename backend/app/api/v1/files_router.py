"""``/api/v1/files`` - thumbnails, raw streaming with HTTP Range, downloads.

Every endpoint takes a **uid**, never a path: the old ``/api/outputs/file?path=``
let the client name any file on disk, which is exactly the traversal shape the
audit flagged.  Paths are resolved from the DB and re-validated against the
configured roots by ``services/file_ops.resolve_file``.
"""

from __future__ import annotations

import email.utils
import mimetypes
import os
import re
import subprocess  # Explorer "reveal" only, never on a scan path
import sys
import urllib.parse
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from ...core.errors import NotFoundError, PathNotAllowed, ValidationError
from ...core.pathsafe import long_path
from ...jobs.thumb_service import get_thumb_service
from ...services import file_ops
from ..deps import check_uid, require_vault_request_always
from ..middleware import ApiError, error_response
from ..schemas.common import BASE_ERRORS, error_responses
from ..schemas.fileops import RevealResponse

router = APIRouter(prefix="/files", tags=["Files"])

CHUNK = 256 * 1024
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
IMMUTABLE = "public, max-age=31536000, immutable"


def _resolve(uid: str) -> dict:
    check_uid(uid)
    try:
        return file_ops.resolve_file(uid)
    except NotFoundError as exc:
        code = "FILE_MISSING" if "no longer on disk" in exc.message else "NOT_FOUND"
        raise ApiError(code, exc.message, details=exc.details) from exc
    except PathNotAllowed as exc:
        raise ApiError("PATH_NOT_ALLOWED", exc.message, details=exc.details) from exc
    except ValidationError as exc:
        raise ApiError("VALIDATION_ERROR", exc.message, details=exc.details) from exc


def _guess_mime(path: str, declared: str | None) -> str:
    if declared:
        return str(declared)
    guessed, _enc = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _file_iter(path: str, start: int, length: int) -> Iterator[bytes]:
    remaining = length
    with open(long_path(path), "rb") as fh:
        fh.seek(start)
        while remaining > 0:
            block = fh.read(min(CHUNK, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Return (start, end) inclusive, or ``None`` when unsatisfiable."""
    match = RANGE_RE.match(header.strip())
    if not match:
        return None
    raw_start, raw_end = match.group(1), match.group(2)
    if not raw_start and not raw_end:
        return None
    if not raw_start:                       # bytes=-500 -> the last 500 bytes
        length = int(raw_end)
        if length <= 0:
            return None
        start = max(0, size - length)
        return start, size - 1
    start = int(raw_start)
    if start >= size:
        return None
    end = int(raw_end) if raw_end else size - 1
    end = min(end, size - 1)
    if end < start:
        return None
    return start, end


def _stream(request: Request, info: dict, *, disposition: str) -> Response:
    path = info["path"]
    try:
        stat = os.stat(long_path(path))
    except OSError as exc:
        raise ApiError("FILE_MISSING", "The file is no longer readable on disk.",
                       details={"uid": info["uid"], "path": path}) from exc
    size = int(stat.st_size)
    mime = _guess_mime(path, info.get("mime"))
    filename = str(info.get("filename") or os.path.basename(path))
    quoted = urllib.parse.quote(filename, safe="")
    etag = f'"{info.get("fingerprint") or int(stat.st_mtime_ns)}-{size}"'

    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff",
        "Last-Modified": email.utils.formatdate(stat.st_mtime, usegmt=True),
        "ETag": etag,
        "Content-Disposition": (
            "inline" if disposition == "inline"
            else f"attachment; filename*=UTF-8''{quoted}"),
    }

    range_header = request.headers.get("range")
    if range_header:
        window = _parse_range(range_header, size)
        if window is None:
            response = error_response(
                "VALIDATION_ERROR", "The requested byte range cannot be satisfied.",
                request, status=416, details={"size": size, "range": range_header})
            response.headers["Content-Range"] = f"bytes */{size}"
            response.headers["Accept-Ranges"] = "bytes"
            return response
        start, end = window
        length = end - start + 1
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(length)
        return StreamingResponse(_file_iter(path, start, length), status_code=206,
                                 media_type=mime, headers=headers)

    headers["Content-Length"] = str(size)
    return StreamingResponse(_file_iter(path, 0, size), status_code=200,
                             media_type=mime, headers=headers)


@router.get("/thumbnail",
            responses={200: {"content": {"image/webp": {}},
                             "description": "WebP thumbnail"},
                       304: {"description": "Not Modified"},
                       **error_responses("NOT_FOUND", "FILE_MISSING",
                                         "PATH_NOT_ALLOWED"),
                       **BASE_ERRORS},
            response_model=None,
            summary="Cached WebP thumbnail (160/320/640); never 500s on bad media")
async def thumbnail(request: Request, uid: str = Query(...),
                    size: int = Query(320)) -> Response:
    check_uid(uid)
    try:
        result = await get_thumb_service().get(uid, size)
    except NotFoundError as exc:
        raise ApiError("NOT_FOUND", exc.message, details={"uid": uid}) from exc

    if request.headers.get("if-none-match") == result.etag:
        return Response(status_code=304, headers={
            "ETag": result.etag, "Cache-Control": IMMUTABLE,
            "X-Thumb-Source": result.source})
    try:
        stat = os.stat(long_path(result.path))
    except OSError as exc:
        raise ApiError("FILE_MISSING", "The generated thumbnail disappeared.",
                       details={"uid": uid}) from exc
    return FileResponse(
        result.path, media_type=result.mime,
        headers={
            "ETag": result.etag,
            "Cache-Control": IMMUTABLE,
            "Last-Modified": email.utils.formatdate(stat.st_mtime, usegmt=True),
            "X-Thumb-Source": result.source,
        })


@router.get("/raw", response_model=None,
            responses={200: {"description": "Full-resolution stream"},
                       206: {"description": "Partial Content (Range)"},
                       416: {"description": "Requested Range Not Satisfiable"},
                       **error_responses("NOT_FOUND", "FILE_MISSING",
                                         "PATH_NOT_ALLOWED"),
                       **BASE_ERRORS},
            summary="Full-resolution stream with mandatory HTTP Range support")
def raw_file(request: Request, uid: str = Query(...)) -> Response:
    return _stream(request, _resolve(uid), disposition="inline")


@router.get("/download", response_model=None,
            responses={200: {"description": "Attachment stream"},
                       206: {"description": "Partial Content (Range)"},
                       416: {"description": "Requested Range Not Satisfiable"},
                       **error_responses("NOT_FOUND", "FILE_MISSING",
                                         "PATH_NOT_ALLOWED"),
                       **BASE_ERRORS},
            summary="Same stream with an RFC 5987 attachment filename")
def download_file(request: Request, uid: str = Query(...)) -> Response:
    return _stream(request, _resolve(uid), disposition="attachment")


@router.get("/reveal", response_model=RevealResponse,
            dependencies=[Depends(require_vault_request_always)],
            responses={**error_responses("NOT_FOUND", "FILE_MISSING",
                                         "PATH_NOT_ALLOWED", "FEATURE_UNAVAILABLE",
                                         "CSRF_HEADER_MISSING"),
                       **BASE_ERRORS},
            summary="Open Explorer with the file selected (Windows only)")
def reveal_file(uid: str = Query(...)) -> dict:
    if sys.platform != "win32":
        raise ApiError("FEATURE_UNAVAILABLE",
                       "Revealing a file in the file manager is Windows-only here.",
                       details={"platform": sys.platform})
    info = _resolve(uid)
    explorer = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "explorer.exe")
    try:
        subprocess.Popen([explorer, f"/select,{info['path']}"],  # noqa: S603
                         close_fds=True)
    except OSError as exc:
        raise ApiError("FEATURE_UNAVAILABLE", f"Explorer could not be started: {exc}",
                       details={"uid": uid}) from exc
    return {"ok": True}
