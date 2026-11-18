"""Patient-scoped retrieval, bounded caches, and local topic preparation.

No audio, Gemini, Chroma, or ONNX imports here. Adapters are injected. Local
model/database work is serialized in the existing foreground-priority worker.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import math
import re
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ember_topics import TopicMatcher


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


@dataclass(frozen=True)
class MemorySettings:
    exact_enabled: bool = False
    prepared_enabled: bool = False
    comparison_enabled: bool = False
    prefetch_enabled: bool = True
    ttl_seconds: float = 300.0
    max_queries: int = 128
    max_documents: int = 256
    max_bytes: int = 8 * 1024 * 1024
    top_k: int = 3
    max_distance: float = 1.0
    prefetch_interval: float = 5.0
    prefetch_top_k: int = 8
    debounce_seconds: float = 0.2
    calibration_path: str = ""
    topic_aliases: dict = field(default_factory=dict)

    def __post_init__(self):
        for name in ("exact_enabled", "prepared_enabled", "comparison_enabled", "prefetch_enabled"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        for name in ("ttl_seconds", "prefetch_interval", "debounce_seconds"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid {name}")
        for name in ("max_queries", "max_documents", "max_bytes", "top_k", "prefetch_top_k"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"invalid {name}")
        if self.prefetch_top_k > 8:
            raise ValueError("prefetch work exceeds the supported bounded limits")
        if not isinstance(self.max_distance, (int, float)) or not math.isfinite(self.max_distance) or not 0 <= self.max_distance <= 2:
            raise ValueError("invalid cosine distance")
        if self.max_queries > 128 or self.max_documents > 256 or self.max_bytes > 8 * 1024 * 1024:
            raise ValueError("cache capacity exceeds the release limits")
        if self.ttl_seconds > 300 or self.prefetch_interval < 5:
            raise ValueError("freshness/rate limits exceed the release limits")
        if not isinstance(self.topic_aliases, dict) or len(self.topic_aliases) > 128:
            raise ValueError("topic_aliases must be a table of at most 128 aliases")
        if any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 160 or len(v) > 160
               for k, v in self.topic_aliases.items()):
            raise ValueError("topic aliases must be short strings")

    @classmethod
    def from_config(cls, config):
        mapping = {
            "cache_enabled": "exact_enabled", "prepared_cache_enabled": "prepared_enabled",
            "cache_comparison_enabled": "comparison_enabled", "prefetch_enabled": "prefetch_enabled",
            "cache_ttl_seconds": "ttl_seconds", "prefetch_interval_seconds": "prefetch_interval",
            "calibration_path": "calibration_path",
            "topic_aliases": "topic_aliases",
        }
        return cls(**{dest: config[src] for src, dest in mapping.items() if src in config})

    def retrieval_identity(self):
        return {"metric": "cosine", "top_k": self.top_k, "max_distance": self.max_distance,
                "preparation": "topic-tags-v1"}


@dataclass
class RetrievalResult:
    records: list
    source: str
    memory_revision: int
    session_id: str
    turn_id: str
    candidate_records: list = field(default_factory=list)
    candidate_score: float | None = None


class PriorityWorker:
    """One non-preemptible local operation at a time; pending foreground wins."""
    def __init__(self):
        self._condition = threading.Condition()
        self._foreground, self._background = deque(), deque()
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="ember-memory", daemon=True)
        self._thread.start()

    def submit(self, fn, *, background=False):
        future = concurrent.futures.Future()
        item = (future, fn)
        with self._condition:
            target = self._background if background else self._foreground
            if self._stopping or len(target) >= (2 if background else 64):
                future.set_exception(RuntimeError("memory worker unavailable"))
            else:
                target.append(item)
                self._condition.notify()
        return future

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopping or self._foreground or self._background)
                if self._stopping and not self._foreground and not self._background:
                    return
                future, fn = (self._foreground or self._background).popleft()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn())
            except BaseException as error:
                future.set_exception(error)

    def close(self):
        with self._condition:
            self._stopping = True
            for target in (self._foreground, self._background):
                while target:
                    target.popleft()[0].cancel()
            self._condition.notify_all()
        self._thread.join()


@dataclass
class _Document:
    record: dict
    vector: np.ndarray | None
    fetched: float


class MemoryService:
    def __init__(self, embedder, patient_id, model_fingerprint, *, settings=None,
                 clock=time.monotonic, record=None):
        if not isinstance(patient_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", patient_id):
            raise ValueError("MemoryService requires an explicit patient identifier")
        self.embedder = embedder
        self.patient_id = patient_id
        self.model_fingerprint = model_fingerprint
        self.settings = settings or MemorySettings()
        self.clock = clock
        self.record = record
        self.worker = PriorityWorker()
        self._lock = threading.RLock()
        self._documents, self._queries = OrderedDict(), OrderedDict()
        self._session_id, self._generation, self._revision = "", 0, 0
        self._closed = False
        self._matcher = TopicMatcher(self.settings.topic_aliases, debounce_seconds=self.settings.debounce_seconds)
        self._observed_turn = None
        self._preparation_epoch = 0
        self._pending = None
        self._preparation_task = self._debounce_task = None
        self._wake = asyncio.Event()
        self._last_attempt = -math.inf
        self._calibration = self._load_calibration()

    def _load_calibration(self):
        if not self.settings.calibration_path:
            return None
        try:
            data = json.loads(Path(self.settings.calibration_path).read_text())
            threshold = data["threshold"]
            if (data["model_fingerprint"] == self.model_fingerprint
                    and data["retrieval_settings"] == self.settings.retrieval_identity()
                    and data["development_passed"] is True and data["heldout_passed"] is True
                    and data.get("backend") == "minilm-chroma"
                    and isinstance(threshold, (int, float)) and not isinstance(threshold, bool)
                    and math.isfinite(threshold) and 0.30 <= threshold <= 0.95):
                return data
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    @property
    def calibrated(self):
        return self._calibration is not None

    def _token(self):
        with self._lock:
            return self.patient_id, self._session_id, self._generation, self._revision

    def _valid(self, token):
        return not self._closed and token == self._token()

    async def start_session(self, session_id):
        await self._cancel_preparation()
        with self._lock:
            self._session_id = session_id
            self._generation += 1
            self._clear()
        self._matcher.reset()
        self._observed_turn = self._pending = None
        # Reconnect must not bypass the five-second background-search budget.

    def invalidate(self, memory_revision=None):
        with self._lock:
            self._revision = self._revision + 1 if memory_revision is None else memory_revision
            self._generation += 1
            self._clear()
        self.discard_preparation()

    def _clear(self):
        self._documents.clear()
        self._queries.clear()

    def _expire(self):
        expired = [key for key, doc in self._documents.items()
                   if self.clock() - doc.fetched >= self.settings.ttl_seconds]
        for key in expired:
            self._evict(key)
        for key, (_, expiry) in list(self._queries.items()):
            if self.clock() >= expiry:
                self._queries.pop(key, None)

    def _evict(self, doc_id):
        self._documents.pop(doc_id, None)
        for key, (records, _) in list(self._queries.items()):
            if any(record[0] == doc_id for record in records):
                del self._queries[key]

    def cache_payload_bytes(self):
        with self._lock:
            return sum(len(str(d.record["document"]).encode()) + len(str(key).encode())
                       + len(json.dumps(d.record.get("metadata"), default=str).encode()) + 256
                       + (d.vector.nbytes if d.vector is not None else 0)
                       for key, d in self._documents.items()) + sum(
                           len(str(key).encode()) + 128 + len(records) * 96
                           for key, (records, _) in self._queries.items())

    def _trim(self):
        while len(self._queries) > self.settings.max_queries:
            self._queries.popitem(last=False)
        while self._documents and (len(self._documents) > self.settings.max_documents
                                  or self.cache_payload_bytes() > self.settings.max_bytes):
            self._evict(next(iter(self._documents)))
        while self._queries and self.cache_payload_bytes() > self.settings.max_bytes:
            self._queries.popitem(last=False)

    def _key(self, query):
        return (self.patient_id, self.model_fingerprint, self._revision,
                self.settings.top_k, self.settings.max_distance, " ".join(query.split()))

    def _remember(self, records):
        for record in records:
            vector = record.get("embedding")
            if vector is not None:
                vector = np.array(vector, dtype=np.float32, copy=True).reshape(-1)
                norm = float(np.linalg.norm(vector))
                vector = vector / norm if norm > 0 and np.isfinite(vector).all() else None
            clean = {k: record.get(k) for k in ("id", "document", "metadata")}
            self._documents[record["id"]] = _Document(clean, vector, self.clock())
            self._documents.move_to_end(record["id"])
        self._trim()

    def _save_query(self, key, records):
        if not self.settings.exact_enabled:
            return
        if all(r["id"] in self._documents for r in records):
            expiry = min((self._documents[r["id"]].fetched + self.settings.ttl_seconds for r in records),
                         default=self.clock() + self.settings.ttl_seconds)
            self._queries[key] = ([(r["id"], r["distance"]) for r in records], expiry)
            self._queries.move_to_end(key)
            self._trim()

    def _exact(self, key):
        item = self._queries.get(key)
        if item is None:
            return None
        records, _ = item
        if any(doc_id not in self._documents for doc_id, _ in records):
            self._queries.pop(key, None)
            return None
        self._queries.move_to_end(key)
        result = []
        for doc_id, distance in records:
            doc = self._documents[doc_id]
            self._documents.move_to_end(doc_id)
            result.append({**doc.record, "distance": distance})
        return result

    def _candidates(self, query_vector):
        docs = [d for d in self._documents.values() if d.vector is not None]
        if len(docs) < self.settings.top_k:
            return [], None
        query = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(query))
        if not norm or not np.isfinite(query).all():
            return [], None
        scores = np.clip(np.stack([d.vector for d in docs]) @ (query / norm), -1, 1)
        result = []
        for index in np.argsort(-scores, kind="stable"):
            distance = float(1 - scores[index])
            if distance <= self.settings.max_distance:
                result.append({**docs[index].record, "distance": distance})
            if len(result) == self.settings.top_k:
                break
        return (result, float(max(scores))) if len(result) == self.settings.top_k else ([], None)

    async def lookup(self, query, session_id, turn_id):
        if session_id != self._session_id or not query.strip() or self._closed:
            return RetrievalResult([], "disabled", self._revision, session_id, turn_id)
        token = self._token()
        queued = time.perf_counter()
        try:
            result = await asyncio.wrap_future(self.worker.submit(
                lambda: self._measured_lookup(query, token, turn_id, queued)))
            if self._valid(token):
                return result
            return RetrievalResult([], "obsolete", self._revision, session_id, turn_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return RetrievalResult([], "error", self._revision, session_id, turn_id)

    def _record(self, name, duration_ms):
        if self.record is not None:
            try:
                self.record(name, duration_ms)
            except Exception:
                pass  # Profiling must not interrupt memory retrieval.

    def _measured_lookup(self, query, token, turn_id, queued):
        started, source = time.perf_counter(), "error"
        try:
            result = self._lookup(query, token, turn_id)
            source = result.source
            return result
        finally:
            elapsed = (time.perf_counter() - queued) * 1000
            self._record("queue", (started - queued) * 1000)
            self._record("retrieval", elapsed)
            self._record("retrieval." + source, elapsed)

    def _lookup(self, query, token, turn_id):
        def result(records, source, candidates=None, score=None):
            clean = [{k: r.get(k) for k in ("id", "document", "distance", "metadata")} for r in records]
            return RetrievalResult(clean, source, token[3], token[1], turn_id, candidates or [], score)
        if not self._valid(token):
            return result([], "obsolete")
        key = self._key(query)
        cached, candidates, score = None, [], None
        try:
            with self._lock:
                self._expire()
                if self.settings.exact_enabled:
                    cached = self._exact(key)
        except Exception:
            with self._lock:
                self._clear()
        if cached is not None:
            return result(cached, "exact_cache")

        vector = self.embedder.embed(query)[0]
        try:
            with self._lock:
                if not self._valid(token):
                    return result([], "obsolete")
                if self.settings.prepared_enabled or self.settings.comparison_enabled:
                    candidates, score = self._candidates(vector)
                if (candidates and self.settings.prepared_enabled and self._calibration
                        and score >= self._calibration["threshold"]):
                    self._save_query(key, candidates)
                    return result(candidates, "prepared_cache", candidates, score)
        except Exception:
            with self._lock:
                self._clear()
            candidates, score = [], None
        if not self._valid(token):
            return result([], "obsolete")
        cache_active = any((self.settings.exact_enabled, self.settings.prepared_enabled,
                            self.settings.comparison_enabled, self.settings.prefetch_enabled))
        records = self.embedder.search_by_embedding(vector, n_results=self.settings.top_k,
            include_embeddings=any((self.settings.prepared_enabled, self.settings.comparison_enabled,
                                    self.settings.prefetch_enabled)))
        records = [r for r in records if r["distance"] <= self.settings.max_distance]
        if self._valid(token) and cache_active:
            with self._lock:
                if self._valid(token):
                    self._remember(records)
                    self._save_query(key, records)
        return result(records, "chroma", candidates, score)

    async def profile_records(self):
        token = self._token()
        def read():
            if not self._valid(token):
                return []
            raw = self.embedder._collection.get(limit=50)
            docs, ids = raw.get("documents") or [], raw.get("ids") or []
            return [{"id": doc_id, "document": text} for doc_id, text in zip(ids, docs)]
        records = await asyncio.wrap_future(self.worker.submit(read))
        if self._valid(token):
            return records
        return []

    def discard_preparation(self):
        """Forget pending/obsolete utterance work, without clearing useful memories."""
        with self._lock:
            self._preparation_epoch += 1
            self._pending = None
        self._observed_turn = None
        self._matcher.reset()
        if self._debounce_task:
            self._debounce_task.cancel()
        self._wake.set()

    def observe_transcript(self, fragment, conversation_context):
        if not self.settings.prefetch_enabled or self._closed:
            return
        if conversation_context["session_id"] != self._session_id:
            return
        turn_id = conversation_context["turn_id"]
        if self._observed_turn != turn_id:
            self.discard_preparation()
            self._observed_turn = turn_id
        cues = self._matcher.feed(fragment, self.clock())
        if cues:
            self._schedule(cues)
        if self._debounce_task:
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounce(turn_id))

    async def _debounce(self, turn_id):
        await asyncio.sleep(self.settings.debounce_seconds)
        if self._observed_turn == turn_id:
            cues = self._matcher.poll(self.clock())
            if cues:
                self._schedule(cues)

    def _schedule(self, cues):
        topics = set(cues)
        if self._pending:
            topics.update(self._pending["topics"])
        self._pending = {"token": self._token(), "turn_id": self._observed_turn,
                         "epoch": self._preparation_epoch, "topics": tuple(sorted(topics))}
        self._wake.set()
        if self._preparation_task is None or self._preparation_task.done():
            self._preparation_task = asyncio.create_task(self._prepare_loop())

    async def _prepare_loop(self):
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            if self._pending is None:
                continue
            delay = self.settings.prefetch_interval - (self.clock() - self._last_attempt)
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), delay)
                except asyncio.TimeoutError:
                    pass
                self._wake.set()
                continue
            snapshot, self._pending = self._pending, None
            try:
                await asyncio.wrap_future(self.worker.submit(
                    lambda: self._prefetch(snapshot), background=True))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"[MEMORY] Topic preparation failed: {type(error).__name__}")

    def _preparation_valid(self, snapshot):
        return self._valid(snapshot["token"]) and snapshot["epoch"] == self._preparation_epoch

    def _prefetch(self, snapshot):
        if not self._preparation_valid(snapshot):
            return
        started = time.perf_counter()
        try:
            self._last_attempt = self.clock()
            filters = [{"tags": {"$contains": topic}} for topic in snapshot["topics"]]
            vector = self.embedder.embed(" ".join(snapshot["topics"]))[0]
            if not self._preparation_valid(snapshot):
                return
            records = self.embedder.search_by_embedding(vector, n_results=self.settings.prefetch_top_k,
                where=filters[0] if len(filters) == 1 else {"$or": filters}, include_embeddings=True)
            with self._lock:
                if self._preparation_valid(snapshot):
                    self._remember(records)
        finally:
            self._record("preparation", (time.perf_counter() - started) * 1000)

    async def _cancel_preparation(self):
        self.discard_preparation()
        tasks = [t for t in (self._preparation_task, self._debounce_task) if t is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._preparation_task = self._debounce_task = None
        self._wake.clear()

    async def close(self):
        self._closed = True
        await self._cancel_preparation()
        await asyncio.to_thread(self.worker.close)
        with self._lock:
            self._clear()


def model_fingerprint(model_dir):
    digest = hashlib.sha256(b"ember-minilm-mean-pooling-float32-v1")
    for name in ("model.onnx", "config.json", "tokenizer.json", "tokenizer_config.json",
                 "special_tokens_map.json", "vocab.txt"):
        path = Path(model_dir) / name
        if path.is_file():
            digest.update(name.encode())
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024*1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()
