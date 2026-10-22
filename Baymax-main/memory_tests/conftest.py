import copy
import threading

import numpy as np


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Events:
    enabled = True

    def __init__(self):
        self.events = []
        self.durations = []

    def event(self, name, session_id="", turn_id="", **attributes):
        self.events.append((name, session_id, turn_id, attributes))

    def duration(self, name, elapsed_ms, session_id="", turn_id="", **attributes):
        self.durations.append((name, elapsed_ms))


class FakeEmbedder:
    def __init__(self, person="alpha"):
        self.embed_calls = []
        self.search_calls = []
        self.entered = threading.Event()
        self.release = None
        self._collection = self
        self.records = []
        for index, vector in enumerate(([1, 0, 0], [.98, .2, 0], [.9, .3, .1],
                                        [0, 1, 0], [.1, .98, .2], [0, .9, .3],
                                        [0, 0, 1], [.1, 0, .98], [.2, .1, .9])):
            vector = np.asarray(vector, dtype=np.float32)
            vector /= np.linalg.norm(vector)
            self.records.append({"id": f"{person}-{index}", "document": f"Sarah garden {person} {index}",
                                 "metadata": {"person": person}, "embedding": vector})

    def embed(self, query):
        self.embed_calls.append(query)
        self.entered.set()
        if self.release:
            self.release.wait(2)
        vector = ([0, 1, 0] if "music" in query else
                  [0, 0, 1] if "doctor" in query else [.8, .6, 0] if "actual" in query else [1, 0, 0])
        return np.asarray([vector], dtype=np.float32)

    def search_by_embedding(self, vector, n_results=3, where=None, include_embeddings=False):
        self.search_calls.append((n_results, include_embeddings))
        vector = np.asarray(vector) / np.linalg.norm(vector)
        records = [{**copy.deepcopy(r), "distance": float(1-np.dot(vector, r["embedding"]))}
                   for r in self.records]
        records.sort(key=lambda r: r["distance"])
        if not include_embeddings:
            for record in records:
                record.pop("embedding")
        return records[:n_results]

    def get(self, limit=50):
        return {"ids": [r["id"] for r in self.records[:limit]],
                "documents": [r["document"] for r in self.records[:limit]]}

    def count(self):
        return len(self.records)
