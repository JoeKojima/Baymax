"""Patient-scoped retrieval, bounded caches, and speculative topic preparation.

No audio, Gemini, Chroma, or ONNX imports here. Adapters are injected. Local
model/database work is serialized in a foreground-priority worker; the network
predictor is independent and is never awaited by a foreground lookup.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import hashlib
import json
import math
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ember_topics import TopicMatcher, prediction_context


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
    prediction_enabled: bool = False
    ttl_seconds: float = 300.0
    max_queries: int = 128
    max_documents: int = 256
    max_bytes: int = 8 * 1024 * 1024
    top_k: int = 3
    max_distance: float = 1.0
    prediction_interval: float = 30.0
    prediction_timeout: float = 3.0
    prediction_topics: int = 2
    prefetch_top_k: int = 8
    debounce_seconds: float = 0.2
    calibration_path: str = ""
    topic_aliases: dict = field(default_factory=dict)

    def __post_init__(self):
        for name in ("exact_enabled", "prepared_enabled", "comparison_enabled", "prediction_enabled"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        for name in ("ttl_seconds", "prediction_interval", "prediction_timeout", "debounce_seconds"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid {name}")
        for name in ("max_queries", "max_documents", "max_bytes", "top_k", "prediction_topics", "prefetch_top_k"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"invalid {name}")
        if self.prediction_topics > 5 or self.prefetch_top_k > 8 or self.prediction_timeout > 3:
            raise ValueError("prediction work exceeds the supported bounded limits")
        if not isinstance(self.max_distance, (int, float)) or not math.isfinite(self.max_distance) or not 0 <= self.max_distance <= 2:
            raise ValueError("invalid cosine distance")
        if self.max_queries > 128 or self.max_documents > 256 or self.max_bytes > 8 * 1024 * 1024:
            raise ValueError("cache capacity exceeds the release limits")
        if self.ttl_seconds > 300 or self.prediction_interval < 30:
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
            "cache_comparison_enabled": "comparison_enabled", "prediction_enabled": "prediction_enabled",
            "cache_ttl_seconds": "ttl_seconds", "prediction_interval_seconds": "prediction_interval",
            "prediction_max_topics": "prediction_topics", "calibration_path": "calibration_path",
            "topic_aliases": "topic_aliases",
        }
        return cls(**{dest: config[src] for src, dest in mapping.items() if src in config})

    def retrieval_identity(self):
        return {"metric": "cosine", "top_k": self.top_k, "max_distance": self.max_distance}


@dataclass
class RetrievalResult:
    records: list
    source: str
    memory_revision: int
    session_id: str
    turn_id: str
    lookup_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    candidate_records: list = field(default_factory=list)
    candidate_score: float | None = None
    retrieval_ms: float = 0.0


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
        item = (future, contextvars.copy_context(), fn, time.perf_counter())
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
                future, context, fn, queued = (self._foreground or self._background).popleft()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(context.run(fn, (time.perf_counter() - queued) * 1000))
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
    prepared: bool
    used: bool = False


class MemoryService:
    def __init__(self, embedder, patient_id, model_fingerprint, *, settings=None,
                 predictor=None, timing=None, clock=time.monotonic, phase=lambda: "unknown"):
        if not isinstance(patient_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", patient_id):
            raise ValueError("MemoryService requires an explicit patient identifier")
        self.embedder = embedder
        self.patient_id = patient_id
        self.model_fingerprint = model_fingerprint
        self.settings = settings or MemorySettings()
        self.predictor = predictor
        self.timing = timing
        self.clock = clock
        self.phase = phase
        self.worker = PriorityWorker()
        self._lock = threading.RLock()
        self._documents, self._queries = OrderedDict(), OrderedDict()
        self._session_id, self._generation, self._revision = "", 0, 0
        self._closed = False
        self._profile_ids = set()
        self._preparation_searches = deque()
        self._matcher_lock = threading.RLock()
        self._matcher = TopicMatcher(self.settings.topic_aliases, debounce_seconds=self.settings.debounce_seconds)
        self._observed_turn = None
        self._context = None
        self._pending = None
        self._prediction_task = self._debounce_task = None
        self._wake = asyncio.Event()
        self._last_attempt = -math.inf
        self._calibration = self._load_calibration()

    def _event(self, name, session_id="", turn_id="", **attributes):
        if self.timing is not None:
            self.timing.event(name, session_id=session_id or self._session_id,
                              turn_id=turn_id, **attributes)

    def _duration(self, name, start, session_id, turn_id):
        elapsed = (time.perf_counter() - start) * 1000
        if self.timing is not None:
            self.timing.duration(name, elapsed, session_id=session_id, turn_id=turn_id)

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
        self._event("cache.calibration_rejected")
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
        await self._cancel_prediction()
        with self._lock:
            self._session_id = session_id
            self._generation += 1
            self._clear()
            self._profile_ids.clear()
        with self._matcher_lock:
            self._matcher.reset()
        self._observed_turn = self._context = self._pending = None
        # Reconnect clears context/caches but must not bypass the request budget.

    def invalidate(self, memory_revision=None):
        with self._lock:
            self._revision = self._revision + 1 if memory_revision is None else memory_revision
            self._generation += 1
            self._clear()
        self._pending = None
        self._event("cache.invalidated")

    def _clear(self):
        for search in self._preparation_searches:
            if not search["used"]:
                self._event("preparation.unused_search")
        self._preparation_searches.clear()
        for document in self._documents.values():
            if document.prepared and not document.used:
                self._event("preparation.unused_record")
        self._documents.clear()
        self._queries.clear()

    def _expire(self):
        while self._preparation_searches and self.clock() - self._preparation_searches[0]["ready"] >= self.settings.ttl_seconds:
            if not self._preparation_searches.popleft()["used"]:
                self._event("preparation.unused_search")
        expired = [key for key, doc in self._documents.items()
                   if self.clock() - doc.fetched >= self.settings.ttl_seconds]
        for key in expired:
            self._evict(key)
        for key, (_, expiry) in list(self._queries.items()):
            if self.clock() >= expiry:
                self._queries.pop(key, None)

    def _evict(self, doc_id):
        document = self._documents.pop(doc_id, None)
        if document and document.prepared and not document.used:
            self._event("preparation.unused_record")
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

    def _remember(self, records, *, prepared=False):
        for record in records:
            vector = record.get("embedding")
            if vector is not None:
                vector = np.array(vector, dtype=np.float32, copy=True).reshape(-1)
                norm = float(np.linalg.norm(vector))
                vector = vector / norm if norm > 0 and np.isfinite(vector).all() else None
            clean = {k: record.get(k) for k in ("id", "document", "metadata")}
            old = self._documents.get(record["id"])
            self._documents[record["id"]] = _Document(clean, vector, self.clock(),
                prepared or bool(old and old.prepared), bool(old and old.used))
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
            doc.used = True
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
        lookup_id = uuid.uuid4().hex
        started = time.perf_counter()
        requested = self.clock()
        try:
            future = self.worker.submit(lambda wait: self._execute_lookup(query, token, turn_id, lookup_id, wait, requested))
            result = await asyncio.wrap_future(future)
            if not self._valid(token):
                return RetrievalResult([], "obsolete", self._revision, session_id, turn_id, lookup_id)
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            self._event("retrieval.error", session_id, turn_id, lookup_id=lookup_id)
            return RetrievalResult([], "error", self._revision, session_id, turn_id, lookup_id)
        finally:
            self._duration("retrieval.await", started, session_id, turn_id)

    def _execute_lookup(self, query, token, turn_id, lookup_id, wait, requested):
        started = time.perf_counter()
        self._active_lookup_started = requested
        try:
            result = self._lookup(query, token, turn_id, lookup_id, wait)
            result.retrieval_ms = wait + (time.perf_counter()-started)*1000
            return result
        finally:
            if self.timing is not None:
                self.timing.duration("retrieval.total", wait + (time.perf_counter()-started)*1000,
                                     session_id=token[1], turn_id=turn_id, lookup_id=lookup_id)

    def _lookup(self, query, token, turn_id, lookup_id, wait):
        session_id, revision = token[1], token[3]
        self._event("retrieval.worker_started", session_id, turn_id, queue_ms=wait, lookup_id=lookup_id)
        if not self._valid(token):
            return RetrievalResult([], "obsolete", revision, session_id, turn_id, lookup_id)
        cached, candidates, score = None, [], None
        key = self._key(query)
        cache_active = any((self.settings.exact_enabled, self.settings.prepared_enabled,
                            self.settings.comparison_enabled, self.settings.prediction_enabled))
        try:
            t = time.perf_counter()
            with self._lock:
                self._expire()
                if self.settings.exact_enabled:
                    cached = self._exact(key)
        except Exception:
            with self._lock:
                self._clear()
            self._event("cache.error", session_id, turn_id)
        finally:
            self._duration("retrieval.exact_check", t, session_id, turn_id)
        if cached is not None:
            return self._result(cached, "exact_cache", token, turn_id, lookup_id)

        t = time.perf_counter()
        vector = self.embedder.embed(query)[0]
        self._duration("retrieval.embedding", t, session_id, turn_id)
        t = time.perf_counter()
        try:
            with self._lock:
                if not self._valid(token):
                    return RetrievalResult([], "obsolete", revision, session_id, turn_id, lookup_id)
                if self.settings.prepared_enabled or self.settings.comparison_enabled:
                    candidates, score = self._candidates(vector)
                if (candidates and self.settings.prepared_enabled and self._calibration
                        and score >= self._calibration["threshold"]):
                    for record in candidates:
                        self._documents[record["id"]].used = True
                    self._save_query(key, candidates)
                    return self._result(candidates, "prepared_cache", token, turn_id, lookup_id,
                                        candidates, score)
        except Exception:
            self._event("cache.error", session_id, turn_id)
            with self._lock:
                self._clear()
            candidates, score = [], None
        finally:
            self._duration("retrieval.cache_lookup", t, session_id, turn_id)
        if not self._valid(token):
            return RetrievalResult([], "obsolete", revision, session_id, turn_id, lookup_id)
        t = time.perf_counter()
        records = self.embedder.search_by_embedding(vector, n_results=self.settings.top_k,
            include_embeddings=any((self.settings.prepared_enabled, self.settings.comparison_enabled,
                                    self.settings.prediction_enabled)))
        self._duration("retrieval.chroma", t, session_id, turn_id)
        records = [r for r in records if r["distance"] <= self.settings.max_distance]
        if self._valid(token) and cache_active:
            try:
                with self._lock:
                    if self._valid(token):
                        self._remember(records)
                        self._save_query(key, records)
            except Exception:
                self._event("cache.error", session_id, turn_id)
        if self.settings.prediction_enabled:
            with self._matcher_lock:
                if self._valid(token):
                    self._matcher.learn(r["document"] for r in records)
        return self._result(records, "chroma", token, turn_id, lookup_id, candidates, score)

    def _result(self, records, source, token, turn_id, lookup_id, candidates=None, score=None):
        clean = [{k: r.get(k) for k in ("id", "document", "distance", "metadata")} for r in records]
        with self._lock:
            if self._valid(token):
                ids = {r["id"] for r in clean}
                for search in self._preparation_searches:
                    if ids.intersection(search["ids"]):
                        search["used"] = True
                        self._event("preparation.coverage", token[1], turn_id,
                                    head_start_ms=(self._active_lookup_started-search["ready"])*1000,
                                    ready_before_lookup=search["ready"] <= self._active_lookup_started)
        self._event("retrieval.ready", token[1], turn_id, source=source, lookup_id=lookup_id,
                    result_count=len(clean), profile_overlap=sum(r["id"] in self._profile_ids for r in clean))
        return RetrievalResult(clean, source, token[3], token[1], turn_id, lookup_id,
                               candidates or [], score)

    async def profile_records(self):
        token = self._token()
        def read(wait):
            if not self._valid(token):
                return []
            raw = self.embedder._collection.get(limit=50)
            docs, ids = raw.get("documents") or [], raw.get("ids") or []
            return [{"id": doc_id, "document": text} for doc_id, text in zip(ids, docs)]
        records = await asyncio.wrap_future(self.worker.submit(read))
        if self._valid(token):
            self._profile_ids = {r["id"] for r in records}
            if self.settings.prediction_enabled:
                with self._matcher_lock:
                    self._matcher.learn(r["document"] for r in records)
            return records
        return []

    def observe_transcript(self, fragment, conversation_context):
        """Context: session_id, turn_id, messages [(role, text)]. Never log it."""
        if not self.settings.prediction_enabled or self.predictor is None or self._closed:
            return
        session_id = conversation_context["session_id"]
        turn_id = conversation_context["turn_id"]
        if session_id != self._session_id:
            return
        with self._matcher_lock:
            if self._observed_turn != turn_id:
                self._matcher.reset()
                self._observed_turn = turn_id
            self._context = dict(conversation_context)
            cues = self._matcher.feed(fragment, self.clock())
            if self._pending is not None and self._pending["turn_id"] == turn_id:
                self._pending["text"] = prediction_context(self._matcher.text, self._context.get("messages", []))
        if cues:
            self._schedule(cues)
        if self._debounce_task:
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounce(turn_id))

    async def _debounce(self, turn_id):
        await asyncio.sleep(self.settings.debounce_seconds)
        if self._observed_turn == turn_id:
            with self._matcher_lock:
                cues = self._matcher.poll(self.clock())
            if cues:
                self._schedule(cues)

    def _schedule(self, cues):
        context = self._context
        self._pending = {
            "token": self._token(), "turn_id": context["turn_id"],
            "snapshot_id": uuid.uuid4().hex, "eligible": self.clock(),
            "text": prediction_context(self._matcher.text, context.get("messages", [])),
        }
        self._event("preparation.cue", turn_id=context["turn_id"], cue_count=len(cues), phase=self.phase())
        self._wake.set()
        if self._prediction_task is None or self._prediction_task.done():
            self._prediction_task = asyncio.create_task(self._predict_loop())

    async def _predict_loop(self):
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            if self._pending is None:
                continue
            delay = self.settings.prediction_interval - (self.clock() - self._last_attempt)
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), delay)
                except asyncio.TimeoutError:
                    pass
                self._wake.set()
                continue
            snapshot, self._pending = self._pending, None
            if not self._valid(snapshot["token"]):
                continue
            self._last_attempt = self.clock()
            sid, tid = snapshot["token"][1], snapshot["turn_id"]
            self._event("prediction.started", sid, tid, phase=self.phase(),
                        throttle_ms=(self.clock()-snapshot["eligible"])*1000,
                        snapshot_id=snapshot["snapshot_id"])
            t = time.perf_counter()
            try:
                phrases = await asyncio.wait_for(
                    self.predictor(snapshot["text"], self.settings.prediction_topics),
                    self.settings.prediction_timeout,
                )
                self._event("prediction.finished", sid, tid, snapshot_id=snapshot["snapshot_id"])
                if not isinstance(phrases, list):
                    raise ValueError("prediction must be a list")
                phrases = list(dict.fromkeys(p.strip()[:160] for p in phrases
                                             if isinstance(p, str) and p.strip()))[:self.settings.prediction_topics]
                for phrase in phrases:
                    if not self._valid(snapshot["token"]):
                        self._event("preparation.obsolete", sid, tid)
                        break
                    await asyncio.wrap_future(self.worker.submit(
                        lambda wait, phrase=phrase: self._prefetch(phrase, snapshot, wait), background=True))
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                self._event("prediction.timeout", sid, tid)
            except Exception:
                self._event("prediction.failed", sid, tid)
            finally:
                self._duration("prediction.total", t, sid, tid)

    def _prefetch(self, phrase, snapshot, wait):
        token, tid = snapshot["token"], snapshot["turn_id"]
        if not self._valid(token):
            return
        self._event("preparation.worker_started", token[1], tid, queue_ms=wait, phase=self.phase())
        t = time.perf_counter()
        vector = self.embedder.embed(phrase)[0]
        if not self._valid(token):
            return
        records = self.embedder.search_by_embedding(vector, n_results=self.settings.prefetch_top_k,
                                                   include_embeddings=True)
        with self._lock:
            if self._valid(token):
                self._remember(records, prepared=True)
                if len(self._preparation_searches) >= 256:
                    if not self._preparation_searches.popleft()["used"]:
                        self._event("preparation.unused_search", token[1], tid)
                self._preparation_searches.append({"ready": self.clock(),
                                                  "ids": {r["id"] for r in records}, "used": False})
                self._event("preparation.ready", token[1], tid, result_count=len(records))
        self._duration("preparation.local", t, token[1], tid)
        with self._matcher_lock:
            if self._valid(token):
                self._matcher.learn(r["document"] for r in records)

    async def _cancel_prediction(self):
        tasks = [t for t in (self._prediction_task, self._debounce_task) if t is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._prediction_task = self._debounce_task = None
        self._wake.clear()

    async def close(self):
        self._closed = True
        await self._cancel_prediction()
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
