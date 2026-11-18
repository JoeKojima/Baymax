"""Bounded patient memory caches and local topic preparation."""
import asyncio
import concurrent.futures
import hashlib
import itertools
import json
import math
import queue
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from ember_topics import TopicMatcher

TTL, QUERY_LIMIT, DOCUMENT_LIMIT, BYTE_LIMIT = 300, 128, 256, 8 * 1024 * 1024
RETRIEVAL = {"metric": "cosine", "top_k": 3, "max_distance": 1.0, "preparation": "topic-tags-v2"}


@dataclass(frozen=True)
class PatientContext:
    patient_id: str
    root: Path

    @classmethod
    def resolve(cls, data_dir, patient_id):
        if not patient_id:
            return None
        if not isinstance(patient_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", patient_id):
            raise ValueError("memory.patient_id must be an opaque filesystem-safe identifier")
        base = Path(data_dir).resolve() / "patients"
        root = base / patient_id
        if root.resolve().parent != base.resolve() or root.is_symlink():
            raise ValueError("patient directory must not escape the patients directory")
        return cls(patient_id, root)

    @property
    def chroma_dir(self):
        return self.root / "chroma_store"

    @property
    def transcript_dir(self):
        return self.root / "transcripts"

    def prepare(self):
        for directory in (self.root, self.chroma_dir, self.transcript_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)


@dataclass
class RetrievalResult:
    records: list
    source: str
    token: int


class PriorityWorker:
    """One local operation at a time, with queued foreground jobs first."""
    def __init__(self):
        self.jobs, self.sequence = queue.PriorityQueue(64), itertools.count()
        self.lock, self.closed = threading.Lock(), False
        self.thread = threading.Thread(target=self._run, daemon=True, name="ember-memory")
        self.thread.start()

    def submit(self, fn, *, background=False):
        future = concurrent.futures.Future()
        with self.lock:
            try:
                if self.closed:
                    raise queue.Full
                self.jobs.put_nowait((int(background), next(self.sequence), fn, future))
            except queue.Full:
                future.set_exception(RuntimeError("memory worker unavailable"))
        return future

    def _run(self):
        while True:
            _, _, fn, future = self.jobs.get()
            if fn is None:
                return
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn())
                except BaseException as error:
                    future.set_exception(error)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            while not self.jobs.empty():
                self.jobs.get_nowait()[3].cancel()
            self.jobs.put_nowait((2, next(self.sequence), None, None))
        self.thread.join()


class MemoryService:
    def __init__(self, embedder, patient_id, fingerprint, *, config=None, clock=time.monotonic, record=None):
        if not isinstance(patient_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", patient_id):
            raise ValueError("MemoryService requires an explicit patient identifier")
        config = config or {}
        self.exact = config.get("cache_enabled", False)
        self.prepared = config.get("prepared_cache_enabled", False)
        self.prefetch = config.get("prefetch_enabled", True)
        self.interval = config.get("prefetch_interval_seconds", 5)
        aliases = config.get("topic_aliases", {})
        if any(type(flag) is not bool for flag in (self.exact, self.prepared, self.prefetch)):
            raise ValueError("memory switches must be booleans")
        if type(self.interval) not in (int, float) or not math.isfinite(self.interval) or self.interval < 5:
            raise ValueError("topic search interval must be at least five seconds")
        if (not isinstance(aliases, dict) or len(aliases) > 128 or any(
                not isinstance(k, str) or not isinstance(v, str) or len(k) > 160 or len(v) > 160
                for k, v in aliases.items())):
            raise ValueError("topic aliases must be at most 128 short string pairs")
        self.embedder, self.clock, self.record = embedder, clock, record
        self.worker, self.lock = PriorityWorker(), threading.RLock()
        self.queries, self.documents = OrderedDict(), OrderedDict()
        self.session, self.version, self.closed = "", 0, False
        self.matcher = TopicMatcher(aliases)
        self.turn, self.prep_version, self.pending = None, 0, None
        self.task = self.debounce = None
        self.wake, self.last_attempt = asyncio.Event(), -math.inf
        self.threshold = self._load_threshold(config.get("calibration_path", ""), fingerprint)

    def _load_threshold(self, path, fingerprint):
        try:
            data = json.loads(Path(path).read_text())
            threshold = data["threshold"]
            if (data.get("model_fingerprint") == fingerprint and data.get("retrieval_settings") == RETRIEVAL
                    and data.get("backend") == "minilm-chroma" and data.get("development_passed") is True
                    and data.get("heldout_passed") is True and type(threshold) in (int, float)
                    and math.isfinite(threshold) and .30 <= threshold <= .95):
                return threshold
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def valid(self, token):
        return not self.closed and token == self.version

    def invalidate(self):
        with self.lock:
            self.version += 1
            self.queries.clear()
            self.documents.clear()
        self.discard_preparation()

    async def start_session(self, session):
        await self._stop_preparation()
        self.invalidate()
        self.session = session

    def _expire(self):
        for cache in (self.queries, self.documents):
            for key, (_, expiry, _) in list(cache.items()):
                if self.clock() >= expiry:
                    del cache[key]

    def _put(self, cache, key, value, limit):
        if isinstance(value, list):
            value = [{k: v for k, v in r.items() if k != "embedding"} for r in value]
        records = value if isinstance(value, list) else [value]
        size = len(str(key).encode()) + 128 + sum(
                   len(json.dumps({k: v for k, v in r.items() if k != "embedding"}, default=str).encode())
                   + getattr(r.get("embedding"), "nbytes", 0) + 256 for r in records)
        if size > BYTE_LIMIT:
            return
        cache[key] = value, self.clock() + TTL, size
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)
        while sum(item[2] for c in (self.queries, self.documents) for item in c.values()) > BYTE_LIMIT:
            (self.documents or self.queries).popitem(last=False)

    def _remember(self, records):
        for record in records:
            if record.get("embedding") is None:
                continue
            vector = np.asarray(record["embedding"], dtype=np.float32).reshape(-1)
            norm = np.linalg.norm(vector)
            if norm > 0 and np.isfinite(vector).all():
                self._put(self.documents, record["id"], {**record, "embedding": vector / norm}, DOCUMENT_LIMIT)

    def _candidates(self, vector):
        docs = [entry[0] for entry in self.documents.values()]
        norm = np.linalg.norm(vector)
        if len(docs) < 3 or not norm or not np.isfinite(vector).all():
            return []
        scores = np.clip(np.stack([r["embedding"] for r in docs]) @ (vector / norm), -1, 1)
        records = [{**docs[i], "distance": float(1 - scores[i])} for i in np.argsort(-scores, kind="stable")[:3]
                   if 1 - scores[i] <= 1.0]
        return records if len(records) == 3 and max(scores) >= self.threshold else []

    def _record(self, name, ms):
        if self.record is not None:
            try:
                self.record(name, ms)
            except Exception:
                pass  # Profiling cannot break retrieval.

    async def lookup(self, query, session):
        token, queued = self.version, time.perf_counter()
        if session != self.session or not query.strip() or self.closed:
            return RetrievalResult([], "disabled", token)
        try:
            result = await asyncio.wrap_future(self.worker.submit(lambda: self._lookup(query, token, queued)))
            return result if self.valid(token) else RetrievalResult([], "obsolete", token)
        except asyncio.CancelledError:
            raise
        except Exception:
            return RetrievalResult([], "error", token)

    def _lookup(self, query, token, queued):
        started, source = time.perf_counter(), "error"
        try:
            records, source = self._retrieve(query, token)
            clean = [{k: v for k, v in r.items() if k != "embedding"} for r in records]
            return RetrievalResult(clean, source, token)
        finally:
            elapsed = (time.perf_counter() - queued) * 1000
            self._record("queue", (started - queued) * 1000)
            self._record("retrieval", elapsed)
            self._record("retrieval." + source, elapsed)

    def _retrieve(self, query, token):
        key = " ".join(query.split())
        with self.lock:
            if not self.valid(token):
                return [], "obsolete"
            self._expire()
            if self.exact and key in self.queries:
                self.queries.move_to_end(key)
                return self.queries[key][0], "exact_cache"
        vector = self.embedder.embed(query)[0]
        with self.lock:
            if not self.valid(token):
                return [], "obsolete"
            if self.prepared and self.threshold is not None:
                try:
                    records = self._candidates(vector)
                except (ValueError, TypeError):
                    self.documents.clear()
                    records = []
                if records:
                    if self.exact:
                        self._put(self.queries, key, records, QUERY_LIMIT)
                    return records, "prepared_cache"
        vectors = self.prepared or self.prefetch
        records = self.embedder.search_by_embedding(vector, n_results=3, include_embeddings=vectors)
        records = [r for r in records if r["distance"] <= 1.0]
        with self.lock:
            if self.valid(token):
                if vectors:
                    self._remember(records)
                if self.exact:
                    self._put(self.queries, key, records, QUERY_LIMIT)
        return records, "chroma"

    async def profile_records(self):
        token = self.version
        raw = await asyncio.wrap_future(self.worker.submit(lambda: self.embedder._collection.get(limit=50)))
        if not self.valid(token):
            return []
        return [{"id": i, "document": d} for i, d in zip(raw.get("ids", []), raw.get("documents", []))]

    def discard_preparation(self):
        with self.lock:
            self.prep_version += 1
            self.pending = self.turn = None
        self.matcher.reset()
        if self.debounce:
            self.debounce.cancel()
        self.wake.set()

    def observe_transcript(self, fragment, session, turn):
        if not self.prefetch or self.closed or session != self.session:
            return
        if turn != self.turn:
            self.discard_preparation()
            self.turn = turn
        self._schedule(self.matcher.feed(fragment, self.clock()))
        if self.debounce:
            self.debounce.cancel()
        self.debounce = asyncio.create_task(self._debounce(turn))

    async def _debounce(self, turn):
        await asyncio.sleep(.2)
        if turn == self.turn:
            self._schedule(self.matcher.poll(self.clock()))

    def _schedule(self, topics):
        if not topics:
            return
        topics = set(topics) | (self.pending[2] if self.pending else set())
        self.pending = self.version, self.prep_version, topics
        self.wake.set()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._prepare())

    async def _prepare(self):
        while self.pending and not self.closed:
            delay = self.interval - (self.clock() - self.last_attempt)
            if delay > 0:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), delay)
                except asyncio.TimeoutError:
                    pass
                continue
            snapshot, self.pending = self.pending, None
            try:
                await asyncio.wrap_future(self.worker.submit(lambda: self._prefetch(snapshot), background=True))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"[MEMORY] Topic preparation failed: {type(error).__name__}")

    def _prefetch(self, snapshot):
        def current():
            return self.valid(snapshot[0]) and snapshot[1] == self.prep_version
        if not current():
            return
        started, self.last_attempt = time.perf_counter(), self.clock()
        try:
            topics = sorted(snapshot[2])
            filters = [{"tags": {"$contains": topic}} for topic in topics]
            vector = self.embedder.embed(" ".join(topics))[0]
            if not current():
                return
            records = self.embedder.search_by_embedding(vector, n_results=8,
                where=filters[0] if len(filters) == 1 else {"$or": filters}, include_embeddings=True)
            with self.lock:
                if current():
                    self._expire()
                    self._remember(records)
        finally:
            self._record("preparation", (time.perf_counter() - started) * 1000)

    async def _stop_preparation(self):
        self.discard_preparation()
        tasks = [task for task in (self.task, self.debounce) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.task = self.debounce = None

    async def close(self):
        self.closed = True
        await self._stop_preparation()
        await asyncio.to_thread(self.worker.close)
        self.queries.clear()
        self.documents.clear()


def model_fingerprint(model_dir):
    digest = hashlib.sha256(b"ember-minilm-mean-pooling-float32-v1")
    for name in ("model.onnx", "config.json", "tokenizer.json", "tokenizer_config.json",
                 "special_tokens_map.json", "vocab.txt"):
        path = Path(model_dir) / name
        if path.is_file():
            digest.update(name.encode())
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()
