import asyncio
import functools
import threading
from contextlib import asynccontextmanager

import numpy as np
from ember_memory import MemoryService


def run_async(fn):
    @functools.wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


@asynccontextmanager
async def memory(config=None, embedder=None, patient="alpha", fingerprint="fake", **options):
    service = MemoryService(embedder or FakeEmbedder(patient), patient, fingerprint, config=config, **options)
    try:
        await service.start_session("s")
        yield service
    finally:
        await service.close()


async def until(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(.002)
    assert predicate(), "background work did not complete"


class Clock:
    now = 100.
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


class FakeEmbedder:
    def __init__(self, person="alpha"):
        self.embed_calls, self.search_calls = [], []
        self.entered, self.release = threading.Event(), None
        self.records = []
        for i, vector in enumerate(([1, 0, 0], [.98, .2, 0], [.9, .3, .1],
                                    [0, 1, 0], [.1, .98, .2], [0, .9, .3],
                                    [0, 0, 1], [.1, 0, .98], [.2, .1, .9])):
            vector = np.asarray(vector, dtype=np.float32)
            self.records.append({"id": f"{person}-{i}", "document": f"Sarah garden {person} {i}",
                "metadata": {"tags": ["family" if i < 3 else "music" if i < 6 else "appointments"]},
                "embedding": vector / np.linalg.norm(vector)})

    def embed(self, text):
        self.embed_calls.append(text)
        self.entered.set()
        if self.release:
            self.release.wait(1)
        return np.asarray([[0, 1, 0] if "music" in text else [0, 0, 1] if "doctor" in text
                           else [.8, .6, 0] if "actual" in text else [1, 0, 0]], dtype=np.float32)

    def search_by_embedding(self, vector, n_results=3, where=None, include_embeddings=False):
        self.search_calls.append((n_results, include_embeddings, where))
        vector = np.asarray(vector) / np.linalg.norm(vector)
        records = [{**r, "distance": float(1 - np.dot(vector, r["embedding"]))}
                   for r in self.records if not where or any(
                       item["tags"]["$contains"] in r["metadata"].get("tags", [])
                       for item in where.get("$or", [where]))]
        records.sort(key=lambda r: r["distance"])
        if not include_embeddings:
            for record in records:
                record.pop("embedding")
        return records[:n_results]
