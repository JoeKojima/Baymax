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


def test_real_chroma_array_filters_and_untagged_patient_stores(tmp_path):
    import chromadb
    assert chromadb.__version__ == "1.5.8"
    def store(patient):
        adapter = SemanticEmbedder.__new__(SemanticEmbedder)
        adapter.verbose = False
        adapter._setup_chroma(tmp_path / patient, "user_memories")
        return adapter
    alpha, beta, empty = store("alpha"), store("beta"), store("empty")
    alpha._collection.add(ids=["family", "both", "music", "legacy"],
        documents=["daughter", "daughter plays piano", "jazz", "untagged memory"],
        embeddings=[[1., 0.], [.9, .1], [0., 1.], [1., 0.]],
        metadatas=[{"tags": ["family"]}, {"tags": ["family", "music"]},
                   {"tags": ["music"]}, {"source": "legacy"}])
    beta._collection.add(ids=["beta-family"], documents=["another patient"],
                         embeddings=[[1., 0.]], metadatas=[{"tags": ["family"]}])
    family = {"tags": {"$contains": "family"}}
    results = alpha.search_by_embedding([1, 0], n_results=8, where=family, include_embeddings=True)
    assert {r["id"] for r in results} == {"family", "both"}
    assert all(r.get("embedding") is not None for r in results)
    either = {"$or": [family, {"tags": {"$contains": "music"}}]}
    assert {r["id"] for r in alpha.search_by_embedding([1, 0], n_results=8, where=either)} == {
        "family", "both", "music"}
    assert alpha.search_by_embedding([1, 0], where={"tags": {"$contains": "pets"}}) == []
    assert "legacy" in {r["id"] for r in alpha.search_by_embedding([1, 0], n_results=8)}
    assert [r["id"] for r in beta.search_by_embedding([1, 0], where=family)] == ["beta-family"]
    assert empty.search_by_embedding([1, 0], where=family) == []
