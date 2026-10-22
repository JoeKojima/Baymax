import numpy as np

from semantic_embedder import SemanticEmbedder


def test_search_existing_vector_preserves_search_and_handles_numpy_embeddings():
    class Collection:
        count_value = 2
        def count(self):
            return self.count_value
        def query(self, **kwargs):
            self.kwargs = kwargs
            return {"ids": [["a", "b"]], "documents": [["first", "second"]],
                    "distances": [[.1, .2]], "metadatas": [[None, {}]],
                    "embeddings": [np.asarray([[1, 0], [0, 1]])]}
    embedder = SemanticEmbedder.__new__(SemanticEmbedder)
    embedder._collection, embedder.verbose = Collection(), False
    embedded = []
    def embed(text):
        embedded.append(text)
        return np.asarray([[1, 0]])
    embedder.embed = embed
    assert embedder.search("question")[0] == {"id": "a", "document": "first", "distance": .1, "metadata": None}
    assert embedded == ["question"]
    results = embedder.search_by_embedding([1, 0], where={"source": "manual"}, include_embeddings=True)
    assert np.array_equal(results[1]["embedding"], [0, 1])
    assert embedded == ["question"]
    assert embedder._collection.kwargs["n_results"] == 2
    assert embedder._collection.kwargs["where"] == {"source": "manual"}
    assert "embeddings" in embedder._collection.kwargs["include"]
    embedder._collection.count_value = 0
    assert embedder.search("empty") == []
    assert embedded == ["question"]
