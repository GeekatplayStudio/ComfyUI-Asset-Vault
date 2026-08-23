"""Local ONNX embeddings: ``all-MiniLM-L6-v2`` INT8, 384 dims, CPU only.

No torch.  If onnxruntime or the model is unavailable the service reports
``state='unavailable'`` with a reason and search silently falls back to FTS5 -
never an error toast (DECISIONS C2).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

from ..config import EMBED_MODEL_DIR
from ..core import config_service, progress
from ..core import db as dbmod
from ..core.errors import FeatureUnavailable

log = logging.getLogger(__name__)

DIM = 384
MODEL_ID = "all-MiniLM-L6-v2-int8"
MAX_TOKENS = 256
INDEX_BATCH = 32
QUERY_CACHE = 512

FILES = {
    "model.onnx": "onnx/model_quantized.onnx",
    "tokenizer.json": "tokenizer.json",
    "config.json": "config.json",
}

STATE_NOT_INSTALLED = "not_installed"
STATE_DOWNLOADING = "downloading"
STATE_READY = "ready"
STATE_UNAVAILABLE = "unavailable"


def _open_write(path):
    return open(path, "wb")


def _np():
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - numpy is a declared dependency
        return None
    return np


class EmbedService:
    def __init__(self, model_dir: Path | None = None) -> None:
        self.dir = Path(model_dir or EMBED_MODEL_DIR)
        self.bus = progress.bus("embed")
        self._lock = threading.RLock()
        self._session = None
        self._tokenizer = None
        self._state = STATE_NOT_INSTALLED
        self._reason: str | None = None
        self._download: dict = {}
        self._query_cache: dict[str, object] = {}
        self._matrix = None
        self._uids: list[str] = []
        self._kinds: list[str] = []
        self._matrix_stamp = 0
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self.refresh_state()

    # -- state ------------------------------------------------------------
    def refresh_state(self) -> str:
        with self._lock:
            if self._state == STATE_DOWNLOADING:
                return self._state
            missing = [n for n in FILES if not (self.dir / n).is_file()]
            if missing:
                self._state = STATE_NOT_INSTALLED
                self._reason = f"Model files not present: {', '.join(missing)}"
                return self._state
            try:
                import onnxruntime  # noqa: F401
                import tokenizers  # noqa: F401
            except ImportError as exc:
                self._state = STATE_UNAVAILABLE
                self._reason = f"onnxruntime/tokenizers unavailable: {exc}"
                return self._state
            self._state = STATE_READY
            self._reason = None
            return self._state

    @property
    def available(self) -> bool:
        return self._state == STATE_READY

    def status(self) -> dict:
        conn = dbmod.get_ro()
        try:
            embedded = int(dbmod.scalar(conn, "SELECT COUNT(*) FROM embeddings") or 0)
            queued = int(dbmod.scalar(conn, "SELECT COUNT(*) FROM embed_queue") or 0)
            total = int(dbmod.scalar(conn, "SELECT COUNT(*) FROM search_docs") or 0)
        except sqlite3.DatabaseError:
            embedded = queued = total = 0
        return {
            "state": self._state, "reason": self._reason, "model_id": MODEL_ID,
            "dim": DIM, "embedded": embedded, "queued": queued, "documents": total,
            "smart_available": self.available and embedded > 0,
            "path": str(self.dir),
            "building": bool(self._worker and self._worker.is_alive()),
            **({"download": self._download} if self._download else {}),
        }

    async def subscribe(self) -> AsyncIterator[tuple[str, dict]]:
        async for item in self.bus.subscribe():
            yield item

    # -- install ----------------------------------------------------------
    async def enable(self, source: str = "auto") -> dict:
        if self.refresh_state() == STATE_READY:
            self.rebuild(None, force=False)
            return self.status()
        if source == "local":
            raise FeatureUnavailable(
                "Smart search model files were not found.",
                details={"path": str(self.dir), "files": list(FILES)},
            )
        cfg = config_service.get_config()
        if not cfg.embedding_model_url:
            raise FeatureUnavailable("No embedding model URL is configured.")
        await self._download_model(cfg.embedding_model_url)
        if self.refresh_state() == STATE_READY:
            config_service.set_config({"embedding_state": STATE_READY,
                                       "smart_search_enabled": True})
            self.rebuild(None, force=False)
        return self.status()

    async def _download_model(self, base_url: str) -> None:
        try:
            import httpx
        except ImportError as exc:
            self._state = STATE_UNAVAILABLE
            self._reason = f"httpx unavailable: {exc}"
            raise FeatureUnavailable(self._reason) from exc
        self.dir.mkdir(parents=True, exist_ok=True)
        self._state = STATE_DOWNLOADING
        self._download = {"bytes_done": 0, "bytes_total": 0, "file": None}
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                for local, remote in FILES.items():
                    target = self.dir / local
                    if target.is_file():
                        continue
                    url = base_url.rstrip("/") + "/" + remote
                    part = target.with_suffix(target.suffix + ".part")
                    self._download["file"] = local
                    async with client.stream("GET", url) as resp:
                        resp.raise_for_status()
                        total = int(resp.headers.get("content-length") or 0)
                        self._download["bytes_total"] = total
                        done = 0
                        h = hashlib.sha256()
                        loop = asyncio.get_running_loop()
                        fh = await loop.run_in_executor(None, _open_write, part)
                        try:
                            async for chunk in resp.aiter_bytes(1 << 16):
                                await loop.run_in_executor(None, fh.write, chunk)
                                h.update(chunk)
                                done += len(chunk)
                                self._download["bytes_done"] = done
                                self.bus.publish("progress", {
                                    "phase": "embed_download", "file": local,
                                    "done": done, "total": total,
                                }, coalesce_key="embed_download")
                        finally:
                            await loop.run_in_executor(None, fh.close)
                    part.replace(target)
                    self._write_manifest(local, h.hexdigest(), done)
        except Exception as exc:  # noqa: BLE001 - any failure degrades, never raises up
            self._state = STATE_UNAVAILABLE
            self._reason = f"Download failed: {exc}"
            config_service.set_config({"embedding_state": STATE_UNAVAILABLE})
            log.warning("embedding model download failed: %s", exc)
            self._download = {}
            return
        self._download = {}

    def _write_manifest(self, name: str, sha: str, size: int) -> None:
        path = self.dir / "MANIFEST.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            data = {}
        data.setdefault("model_id", MODEL_ID)
        data.setdefault("license", "Apache-2.0 (Xenova/all-MiniLM-L6-v2)")
        data.setdefault("files", {})[name] = {"sha256": sha, "bytes": size}
        with contextlib.suppress(OSError):
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def disable(self, purge: bool = False) -> dict:
        config_service.set_config({"smart_search_enabled": False})
        if purge:
            def _op(conn: sqlite3.Connection) -> None:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM embeddings")
                conn.execute("DELETE FROM embed_queue")
                conn.commit()

            try:
                dbmod.writer().run(_op)
            except BaseException as exc:  # noqa: BLE001
                log.debug("ignored (best effort): %s", exc)
            self._invalidate_matrix()
        return self.status()

    # -- inference --------------------------------------------------------
    def _ensure_session(self) -> bool:
        with self._lock:
            if self._session is not None and self._tokenizer is not None:
                return True
            if self.refresh_state() != STATE_READY:
                return False
            try:
                import onnxruntime as ort
                from tokenizers import Tokenizer

                so = ort.SessionOptions()
                so.intra_op_num_threads = min(4, os.cpu_count() or 4)
                so.log_severity_level = 3
                self._session = ort.InferenceSession(
                    str(self.dir / "model.onnx"),
                    providers=["CPUExecutionProvider"], sess_options=so)
                tok = Tokenizer.from_file(str(self.dir / "tokenizer.json"))
                tok.enable_truncation(max_length=MAX_TOKENS)
                tok.enable_padding(length=None)
                self._tokenizer = tok
            except Exception as exc:  # noqa: BLE001 - degrade, never crash search
                self._state = STATE_UNAVAILABLE
                self._reason = f"Could not load the ONNX model: {exc}"
                self._session = None
                self._tokenizer = None
                return False
            return True

    def embed_texts(self, texts: list[str]):
        np = _np()
        if np is None or not texts or not self._ensure_session():
            return None
        try:
            encoded = self._tokenizer.encode_batch(texts)
            ids = np.array([e.ids for e in encoded], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            names = {i.name for i in self._session.get_inputs()}
            if "token_type_ids" in names:
                feed["token_type_ids"] = np.zeros_like(ids)
            feed = {k: v for k, v in feed.items() if k in names}
            out = self._session.run(None, feed)[0]
            m = mask.astype(np.float32)[..., None]
            summed = (out * m).sum(axis=1)
            counts = np.clip(m.sum(axis=1), 1e-9, None)
            vecs = summed / counts
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            return (vecs / np.clip(norms, 1e-9, None)).astype(np.float32)
        except Exception as exc:  # noqa: BLE001 - inference failure degrades to lexical
            log.warning("embedding inference failed: %s", exc)
            self._state = STATE_UNAVAILABLE
            self._reason = f"Inference failed: {exc}"
            return None

    def embed_query(self, text: str):
        if not text:
            return None
        key = hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=8).hexdigest()
        cached = self._query_cache.get(key)
        if cached is not None:
            return cached
        vecs = self.embed_texts([text])
        if vecs is None or not len(vecs):
            return None
        vec = vecs[0]
        if len(self._query_cache) >= QUERY_CACHE:
            self._query_cache.clear()
        self._query_cache[key] = vec
        return vec

    # -- index build ------------------------------------------------------
    def rebuild(self, kinds: list[str] | None = None, force: bool = False) -> str:
        if not self.available:
            return "unavailable"
        if self._worker and self._worker.is_alive():
            return "running"
        self._cancel = threading.Event()
        self._worker = threading.Thread(target=self._build, args=(kinds, force),
                                        name="vault-embed", daemon=True)
        self._worker.start()
        return "started"

    def _pending(self, kinds: list[str] | None, force: bool) -> list[tuple[str, str, str]]:
        from ..indexing.phases import index as index_phase

        conn = dbmod.get_ro()
        docs = index_phase._build_docs(conn)
        have: dict[str, str] = {}
        for r in dbmod.rows(conn, "SELECT uid, text_hash FROM embeddings"):
            have[str(r["uid"])] = str(r["text_hash"])
        out = []
        for d in docs:
            if kinds and d.kind not in kinds:
                continue
            if not force and have.get(d.uid) == d.text_hash:
                continue
            out.append((d.uid, d.kind, d.embed_text, d.text_hash))
        return out

    def _build(self, kinds: list[str] | None, force: bool) -> None:
        np = _np()
        if np is None:
            return
        t0 = time.perf_counter()
        try:
            pending = self._pending(kinds, force)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not enumerate embedding work: %s", exc)
            return
        total = len(pending)
        done = 0
        for start in range(0, total, INDEX_BATCH):
            if self._cancel.is_set():
                break
            chunk = pending[start:start + INDEX_BATCH]
            vecs = self.embed_texts([c[2] for c in chunk])
            if vecs is None:
                break
            rows = [
                (uid, kind, MODEL_ID, DIM, vecs[i].tobytes(), thash, dbmod.now_ms())
                for i, (uid, kind, _text, thash) in enumerate(chunk)
            ]

            def _op(conn: sqlite3.Connection, rows=rows) -> None:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(
                    "INSERT INTO embeddings(uid,kind,model_id,dim,vec,text_hash,created_at) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET "
                    "kind=excluded.kind, model_id=excluded.model_id, dim=excluded.dim, "
                    "vec=excluded.vec, text_hash=excluded.text_hash, "
                    "created_at=excluded.created_at", rows,
                )
                conn.executemany("DELETE FROM embed_queue WHERE uid = ?",
                                 [(r[0],) for r in rows])
                conn.commit()

            try:
                dbmod.writer().run(_op)
            except BaseException as exc:  # noqa: BLE001
                log.warning("could not persist embeddings: %s", exc)
                break
            done += len(chunk)
            self.bus.publish("progress", {
                "phase": "embed", "done": done, "total": total,
                "eta_ms": progress.eta_ms(done, total, time.perf_counter() - t0),
            }, coalesce_key="embed")
        self.bus.flush("embed")
        self._invalidate_matrix()
        self.bus.publish("done", {"phase": "embed", "embedded": done, "total": total,
                                  "duration_ms": int((time.perf_counter() - t0) * 1000)})

    def cancel_build(self) -> None:
        self._cancel.set()

    # -- vector matrix ----------------------------------------------------
    def _invalidate_matrix(self) -> None:
        with self._lock:
            self._matrix = None
            self._uids = []
            self._kinds = []

    def matrix(self):
        """(matrix, uids, kinds) loaded once and cached in RAM."""
        np = _np()
        if np is None:
            return None, [], []
        with self._lock:
            if self._matrix is not None:
                return self._matrix, self._uids, self._kinds
            try:
                conn = dbmod.get_ro()
                rows = dbmod.rows(
                    conn, "SELECT uid, kind, vec FROM embeddings WHERE model_id = ? "
                    "AND dim = ? ORDER BY uid", (MODEL_ID, DIM))
            except sqlite3.DatabaseError:
                return None, [], []
            if not rows:
                return None, [], []
            uids: list[str] = []
            kinds: list[str] = []
            buf = bytearray()
            for r in rows:
                blob = r["vec"]
                if not isinstance(blob, (bytes, bytearray)) or len(blob) != DIM * 4:
                    continue
                uids.append(str(r["uid"]))
                kinds.append(str(r["kind"]))
                buf.extend(blob)
            if not uids:
                return None, [], []
            m = np.frombuffer(bytes(buf), dtype=np.float32).reshape(len(uids), DIM)
            self._matrix = m
            self._uids = uids
            self._kinds = kinds
            self._matrix_stamp = dbmod.now_ms()
            return m, uids, kinds

    # SCALE NOTE: brute-force matmul is ~1-2 ms at N=10k.  Revisit with IVF or
    # sqlite-vec only above N > 150_000 (ARCHITECTURE 5.3).
    def search(self, query: str, *, kinds: list[str] | None = None,
               limit: int = 200) -> list[tuple[str, str, float]]:
        np = _np()
        if np is None or not self.available:
            return []
        q = self.embed_query(query)
        if q is None:
            return []
        m, uids, doc_kinds = self.matrix()
        if m is None or not len(uids):
            return []
        scores = m @ q
        k = min(int(limit) * (3 if kinds else 1), len(uids))
        if k <= 0:
            return []
        idx = np.argpartition(-scores, k - 1)[:k]
        idx = idx[np.argsort(-scores[idx])]
        out: list[tuple[str, str, float]] = []
        for i in idx:
            kind = doc_kinds[i]
            if kinds and kind not in kinds:
                continue
            out.append((uids[i], kind, float(scores[i])))
            if len(out) >= limit:
                break
        return out


_service: EmbedService | None = None
_lock = threading.Lock()


def get_embed_service() -> EmbedService:
    global _service
    with _lock:
        if _service is None:
            _service = EmbedService()
        return _service


def shutdown_embed_service() -> None:
    global _service
    with _lock:
        if _service is not None:
            _service.cancel_build()
            _service = None
