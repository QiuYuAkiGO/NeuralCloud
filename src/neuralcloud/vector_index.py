"""Optional local embedding worker and disposable SQLite/matrix index.

Never imported by the ordinary memory service. Requires the retrieval extra.
"""

from collections import OrderedDict
from pathlib import Path
import argparse
import hashlib
import json
import sqlite3
import secrets
import logging
import threading
import time

import numpy as np


class Encoder:
    def __init__(self, model, revision, *, backend="torch", threads=4):
        import torch
        from sentence_transformers import SentenceTransformer

        torch.set_num_threads(threads)
        kwargs = {}
        if backend == "torch-bf16":
            kwargs = {"model_kwargs": {"torch_dtype": torch.bfloat16}}
        if backend.startswith("onnx"):
            import onnxruntime

            session_options = onnxruntime.SessionOptions()
            session_options.intra_op_num_threads = threads
            session_options.inter_op_num_threads = 1
            kwargs = {"model_kwargs": {"provider": "CPUExecutionProvider", "session_options": session_options}}
        if backend == "onnx-int8":
            kwargs["model_kwargs"]["file_name"] = "onnx/model_quantized.onnx"
        self.model = SentenceTransformer(
            model,
            revision=revision,
            device="cpu",
            backend="onnx" if backend == "onnx-int8" else ("torch" if backend == "torch-bf16" else backend),
            trust_remote_code=False,
            **kwargs,
        )
        self.model.max_seq_length = 512
        self.name, self.revision, self.backend = Path(model).name if Path(model).is_dir() else model, revision, backend
        self.tokenizer = self.model.tokenizer

    def encode(self, texts, *, query=False):
        if query:
            if "Qwen3" in self.name:
                texts = [
                    "Instruct: Retrieve personal memory evidence relevant to this conversation query.\nQuery: " + t
                    for t in texts
                ]
            elif "bge-small-zh" in self.name:
                texts = ["为这个句子生成表示以用于检索相关文章：" + t for t in texts]
        return self.model.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True, batch_size=16, show_progress_bar=False
        ).astype("float32")

    def chunks(self, text):
        offsets = self.tokenizer(text, add_special_tokens=False, return_offsets_mapping=True, truncation=False)[
            "offset_mapping"
        ]
        if not offsets:
            return [(0, len(text), text)]
        chunks = []
        for start in range(0, len(offsets), 320):
            end = min(start + 384, len(offsets))
            a, b = offsets[start][0], offsets[end - 1][1]
            if b > a:
                chunks.append((a, b, text[a:b]))
            if end == len(offsets):
                break
        return chunks


class VectorIndex:
    def __init__(self, source, target, encoder):
        self.source, self.target, self.encoder = Path(source), Path(target), encoder
        self.encode_lock = threading.Lock()
        self.lock = threading.RLock()
        self.cache = OrderedDict()
        self.waiting = 0
        self.last_error = None
        self.rows, self.matrix = [], np.empty((0, 0), dtype="float32")
        with self.source_connection() as src:
            self.database_id = src.execute("SELECT value FROM metadata WHERE key='database_id'").fetchone()[0]
        self.signature = json.dumps(
            [self.database_id, encoder.name, encoder.revision, encoder.backend, "tokens384-overlap64-v1"]
        )
        self.target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.target) as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE IF NOT EXISTS chunks(record_id TEXT,revision INTEGER,hash TEXT,start INTEGER,end INTEGER,vector BLOB,PRIMARY KEY(record_id,start));"
            )
            signature = db.execute("SELECT value FROM metadata WHERE key='signature'").fetchone()
            if signature and signature[0] != self.signature:
                raise ValueError("VECTOR_MODEL_OR_DATABASE_MISMATCH")
            db.execute("INSERT OR IGNORE INTO metadata VALUES('signature',?)", (self.signature,))
            db.execute("INSERT OR IGNORE INTO metadata VALUES('cursor','0')")
        self.reload()

    def source_connection(self):
        return sqlite3.connect(self.source.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)

    def reload(self):
        with sqlite3.connect(self.target) as db:
            data = db.execute(
                "SELECT record_id,revision,start,end,vector FROM chunks ORDER BY record_id,start"
            ).fetchall()
        matrix = (
            np.stack([np.frombuffer(r[4], dtype="float32") for r in data])
            if data
            else np.empty((0, 0), dtype="float32")
        )
        with self.lock:
            self.rows = [r[:4] for r in data]
            self.matrix = matrix

    def status(self):
        with sqlite3.connect(self.target) as db:
            cursor = int(db.execute("SELECT value FROM metadata WHERE key='cursor'").fetchone()[0])
        with self.source_connection() as db:
            current = db.execute("SELECT coalesce(max(seq),0) FROM changes").fetchone()[0]
        return {
            "status": "READY" if self.rows else "WARMING",
            "index_cursor": cursor,
            "generation": current,
            "lag": max(0, current - cursor),
            "chunks": len(self.rows),
            "last_error": self.last_error,
            "model": self.encoder.name,
            "revision": self.encoder.revision,
            "backend": self.encoder.backend,
        }

    def sync_batch(self, batch=64, encode_batch=4):
        with sqlite3.connect(self.target) as db:
            cursor = int(db.execute("SELECT value FROM metadata WHERE key='cursor'").fetchone()[0])
        with self.source_connection() as src:
            src.execute("BEGIN")
            changes = src.execute(
                "SELECT seq,record_id FROM changes WHERE seq>? ORDER BY seq LIMIT ?", (cursor, batch)
            ).fetchall()
            if not changes:
                return 0
            records = []
            for rid in dict.fromkeys(r[1] for r in changes):
                row = src.execute(
                    "SELECT r.revision,r.status,v.payload FROM records r JOIN revisions v ON r.id=v.record_id AND r.revision=v.revision WHERE r.id=?",
                    (rid,),
                ).fetchone()
                records.append((rid, row))
        staged = []
        work = []
        with sqlite3.connect(self.target) as db:
            previous = {
                rid: db.execute(
                    "SELECT revision,hash,start,end FROM chunks WHERE record_id=? ORDER BY start", (rid,)
                ).fetchall()
                for rid, _ in records
            }
        for rid, row in records:
            values = []
            if row and row[1] == "ONLINE":
                content = json.loads(row[2])["content"]
                chunks = self.encoder.chunks(content)
                digest = hashlib.sha256(content.encode()).hexdigest()
                if previous[rid] == [(row[0], digest, a, b) for a, b, _ in chunks]:
                    continue
                for a, b, text in chunks:
                    work.append((values, (rid, row[0], digest, a, b), text))
            staged.append((rid, values))
        for start in range(0, len(work), encode_batch):
            items = work[start : start + encode_batch]
            while self.waiting:
                time.sleep(0.01)
            with self.encode_lock:
                vectors = self.encoder.encode([item[2] for item in items])
            if vectors.ndim != 2 or not np.isfinite(vectors).all():
                raise ValueError("INVALID_EMBEDDING_OUTPUT")
            for (values, metadata, _), vec in zip(items, vectors, strict=True):
                values.append((*metadata, vec.tobytes()))
        with sqlite3.connect(self.target) as db:
            for rid, values in staged:
                db.execute("DELETE FROM chunks WHERE record_id=?", (rid,))
                db.executemany("INSERT INTO chunks VALUES(?,?,?,?,?,?)", values)
            db.execute("UPDATE metadata SET value=? WHERE key='cursor'", (str(changes[-1][0]),))
        if staged:
            self.reload()
        return len(changes)

    def search(self, query, limit=50, timeout=1.2):
        deadline = time.monotonic() + timeout
        with self.lock:
            if not self.rows:
                raise RuntimeError("INDEX_NOT_READY")
            vector = self.cache.get(query)
            self.waiting += 1
        try:
            if vector is None:
                if not self.encode_lock.acquire(timeout=max(0, deadline - time.monotonic())):
                    raise TimeoutError("ENCODER_BUSY")
                try:
                    vector = self.encoder.encode([query], query=True)[0]
                finally:
                    self.encode_lock.release()
                with self.lock:
                    self.cache[query] = vector
                    while len(self.cache) > 256:
                        self.cache.popitem(last=False)
        finally:
            with self.lock:
                self.waiting -= 1
        if time.monotonic() > deadline:
            raise TimeoutError("EMBEDDING_DEADLINE")
        with self.lock:
            rows, matrix = self.rows, self.matrix
        similarities = matrix @ vector
        selected = np.argsort(-similarities)
        result, seen = [], set()
        # Filter stale revisions BEFORE top-k selection, including changes pending re-embedding.
        with self.source_connection() as src:
            for idx in selected:
                if time.monotonic() > deadline:
                    raise TimeoutError("SEARCH_DEADLINE")
                rid, revision, a, b = rows[idx]
                if rid in seen:
                    continue
                live = src.execute("SELECT revision,status FROM records WHERE id=?", (rid,)).fetchone()
                if live != (revision, "ONLINE"):
                    continue
                seen.add(rid)
                result.append(
                    {"record_id": rid, "revision": revision, "start": a, "end": b, "score": float(similarities[idx])}
                )
                if len(result) >= limit:
                    break
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--backend", default="torch", choices=["torch", "torch-bf16", "onnx", "onnx-int8"])
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args()
    from .ownership import Ownership

    owner = Ownership(args.index.with_suffix(args.index.suffix + ".owner"))
    encoder = Encoder(args.model, args.revision, backend=args.backend)
    index = VectorIndex(args.source, args.index, encoder)
    encoder.encode(["预热"], query=True)
    if args.build_only:
        try:
            while index.sync_batch(encode_batch=16):
                pass
        finally:
            owner.close()
        return
    import uvicorn
    from fastapi import FastAPI, HTTPException, Header, Depends
    from pydantic import BaseModel, Field, ConfigDict

    class Search(BaseModel):
        model_config = ConfigDict(extra="forbid")
        query: str = Field(min_length=1, max_length=256)
        limit: int = Field(default=50, ge=1, le=50)

    if not args.token_file:
        raise ValueError("SEMANTIC_TOKEN_FILE_REQUIRED")
    token = args.token_file.read_text().strip()
    if len(token) < 32:
        raise ValueError("SEMANTIC_CREDENTIAL_REQUIRED")

    async def authenticate(authorization: str = Header(default="")):
        if not secrets.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401, "UNAUTHORIZED")

    app = FastAPI(docs_url=None, openapi_url=None, redoc_url=None, dependencies=[Depends(authenticate)])

    @app.get("/health")
    def health():
        return index.status()

    @app.post("/search")
    def search(body: Search):
        try:
            return {"status": "READY", "database_id": index.database_id, "items": index.search(body.query, body.limit)}
        except (TimeoutError, RuntimeError):
            raise HTTPException(503, "SEMANTIC_NOT_READY")

    stop = threading.Event()

    def follow():
        while not stop.is_set():
            try:
                count = index.sync_batch()
                index.last_error = None
            except Exception as exc:
                marker = type(exc).__name__
                if index.last_error != marker:
                    logging.getLogger(__name__).error("Semantic index sync failed: %s", marker)
                index.last_error = marker
                count = 0
            stop.wait(0.05 if count else 1)

    worker = threading.Thread(target=follow, daemon=True)
    worker.start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)
    finally:
        stop.set()
        worker.join(timeout=30)
        if worker.is_alive():
            raise RuntimeError("SEMANTIC_WORKER_SHUTDOWN_INCOMPLETE")
        owner.close()


if __name__ == "__main__":
    main()
