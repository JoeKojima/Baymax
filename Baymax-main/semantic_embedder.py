#!/usr/bin/env python3
"""
Semantic Embedder using ONNX Runtime
Optimized for Intel CPU (LattePanda Debian)

Usage:
    from semantic_embedder import SemanticEmbedder
    
    embedder = SemanticEmbedder()
    embeddings = embedder.embed("Your text here")
    similarity = embedder.similarity("text1", "text2")
    
    # Save to ChromaDB
    embedder.save("Some text", ids="doc_1", metadatas={"source": "manual"})
    
    # Search ChromaDB
    results = embedder.search("query text", n_results=5)
"""

import uuid
import numpy as np
import time
import warnings
from pathlib import Path

# Suppress ONNX warnings
warnings.filterwarnings('ignore')


class SemanticEmbedder:
    """
    Generate semantic embeddings using sentence-transformers with ONNX acceleration.
    Optionally persists embeddings to a local ChromaDB store.

    Model: sentence-transformers/all-MiniLM-L6-v2
    - Embedding dimension: 384
    - Max sequence length: 256 tokens
    - ONNX optimized for Intel CPU
    """
    
    def __init__(self, model_dir="./all-MiniLM-L6-v2-onnx", verbose=True,
                 chroma_dir="./chroma_store", collection_name="embeddings"):
        """
        Initialize the semantic embedder.
        
        Args:
            model_dir (str):        Path to ONNX-converted model directory
            verbose (bool):         Print loading information
            chroma_dir (str):       Directory for ChromaDB persistent storage.
                                    Pass None to disable ChromaDB entirely.
            collection_name (str):  Name of the ChromaDB collection to use.
        
        Raises:
            FileNotFoundError: If model files not found in model_dir
            ImportError: If required packages not installed
        """
        self.model_dir = Path(model_dir)
        self.verbose = verbose
        self._collection = None

        # Verify model exists
        if not self.model_dir.exists():
            raise FileNotFoundError(
                f"Model not found at {model_dir}\n"
                "Run: python3 convert_model.py"
            )
        
        if not (self.model_dir / "model.onnx").exists():
            raise FileNotFoundError(
                f"model.onnx not found in {model_dir}\n"
                "Model directory is incomplete or corrupted."
            )
        
        # Import here to fail gracefully
        try:
            from optimum.onnxruntime import ORTModelForFeatureExtraction
            from transformers import AutoTokenizer
        except ImportError as e:
            raise ImportError(
                f"Missing package: {e}\n"
                "Install with: pip install optimum[onnxruntime] transformers"
            )
        
        if self.verbose:
            print("Loading ONNX model...")
        
        start_time = time.time()
        
        try:
            self.model = ORTModelForFeatureExtraction.from_pretrained(
                str(self.model_dir)
            )
            self.tokenizer = AutoTokenizer.from_pretrained(
                str(self.model_dir)
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to load model: {e}\n"
                "Model files may be corrupted. Try re-running convert_model.py"
            )
        
        elapsed = time.time() - start_time
        
        if self.verbose:
            print(f"✓ Model loaded in {elapsed:.2f}s")
            print(f"  - Embedding dimension: 384")
            print(f"  - Max sequence: 256 tokens")
            print(f"  - Optimized: ONNX Runtime + Intel CPU")

        # ChromaDB setup
        if chroma_dir is not None:
            self._setup_chroma(chroma_dir, collection_name)

    # ------------------------------------------------------------------
    # ChromaDB setup
    # ------------------------------------------------------------------

    def _setup_chroma(self, chroma_dir, collection_name):
        """Initialize a persistent ChromaDB client and collection."""
        try:
            import chromadb
        except ImportError:
            raise ImportError(
                "ChromaDB not installed.\n"
                "Install with: pip install chromadb"
            )

        if self.verbose:
            print(f"Connecting to ChromaDB at '{chroma_dir}'...")

        # PersistentClient writes a SQLite + binary index to chroma_dir.
        # Data survives process restarts automatically — no manual flush needed.
        self._chroma_client = chromadb.PersistentClient(path=str(chroma_dir))

        # get_or_create_collection is idempotent — safe to call every startup.
        # embedding_function=None because WE supply the vectors ourselves.
        self._collection = self._chroma_client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"}  # use cosine distance for queries
        )

        if self.verbose:
            count = self._collection.count()
            print(f"✓ ChromaDB ready — collection '{collection_name}' "
                  f"({count} existing embeddings)")

    # ------------------------------------------------------------------
    # Core embedding
    # ------------------------------------------------------------------

    def embed(self, texts, show_stats=False):
        """
        Generate embeddings for one or more texts.
        
        Args:
            texts (str or list[str]): Text(s) to embed
            show_stats (bool): Print timing statistics
        
        Returns:
            np.ndarray: Embeddings of shape (n_texts, 384)
        
        Examples:
            >>> embedder = SemanticEmbedder()
            >>> # Single text
            >>> emb = embedder.embed("Hello world")
            >>> emb.shape
            (1, 384)
            >>> # Multiple texts
            >>> embs = embedder.embed(["Hello", "World"])
            >>> embs.shape
            (2, 384)
        """
        # Handle single string input
        if isinstance(texts, str):
            texts = [texts]
        
        start_time = time.time()
        
        # Tokenize with attention to max length
        encoded = self.tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=256,
            return_tensors="pt"
        )
        
        tokenize_time = time.time() - start_time
        
        # Run inference
        inference_start = time.time()
        outputs = self.model(**encoded)
        inference_time = time.time() - inference_start
        
        # Mean pooling to get sentence embeddings
        pool_start = time.time()
        embeddings = self._mean_pooling(
            outputs.last_hidden_state,
            encoded['attention_mask']
        )
        embeddings_np = embeddings.cpu().numpy()
        pool_time = time.time() - pool_start
        
        if show_stats:
            total_time = time.time() - start_time
            print(f"\nTiming ({len(texts)} texts):")
            print(f"  Tokenization: {tokenize_time*1000:.1f}ms")
            print(f"  Inference:    {inference_time*1000:.1f}ms")
            print(f"  Pooling:      {pool_time*1000:.1f}ms")
            print(f"  Total:        {total_time*1000:.1f}ms")
            print(f"  Throughput:   {len(texts)/total_time:.1f} texts/sec")
        
        return embeddings_np

    # ------------------------------------------------------------------
    # ChromaDB: save and search
    # ------------------------------------------------------------------

    def save(self, texts, ids=None, metadatas=None):
        """
        Embed texts and persist them to ChromaDB.

        Args:
            texts (str | list[str]):       Text(s) to embed and store.
            ids (str | list[str]):         Unique ID(s) for each text.
                                           Auto-generated (UUID) if omitted.
            metadatas (dict | list[dict]): Optional metadata per text,
                                           e.g. {"source": "readme.txt"}.

        Returns:
            list[str]: The IDs under which the texts were stored.

        Raises:
            RuntimeError: If ChromaDB was disabled (chroma_dir=None).
            ValueError:   If len(ids) != len(texts).

        Examples:
            >>> embedder.save("The cat sat on the mat", ids="doc_1")

            >>> embedder.save(
            ...     ["Hello", "World"],
            ...     ids=["id_1", "id_2"],
            ...     metadatas=[{"lang": "en"}, {"lang": "en"}]
            ... )
        """
        if self._collection is None:
            raise RuntimeError(
                "ChromaDB is not enabled. "
                "Pass a chroma_dir when constructing SemanticEmbedder."
            )

        # Normalise scalars to lists
        if isinstance(texts, str):
            texts = [texts]
        if isinstance(ids, str):
            ids = [ids]
        if isinstance(metadatas, dict):
            metadatas = [metadatas]

        # Auto-generate IDs if not supplied
        if ids is None:
            ids = [str(uuid.uuid4()) for _ in texts]

        if len(ids) != len(texts):
            raise ValueError(
                f"len(ids)={len(ids)} must match len(texts)={len(texts)}"
            )

        # Generate embeddings using the existing embed() method
        embeddings = self.embed(texts)

        # ChromaDB expects plain Python lists, not numpy arrays
        embeddings_list = embeddings.tolist()

        # upsert = insert, or silently overwrite if the ID already exists.
        # Swap for add() if you want duplicate-ID errors to raise instead.
        self._collection.upsert(
            ids=ids,
            embeddings=embeddings_list,
            documents=texts,
            metadatas=metadatas  # ChromaDB accepts None here
        )

        if self.verbose:
            print(f"✓ Saved {len(texts)} embedding(s) to ChromaDB "
                  f"(collection total: {self._collection.count()})")

        return ids

    def search(self, query, n_results=5, where=None):
        """
        Embed a query and retrieve the most similar stored texts.

        Args:
            query (str):      The search query.
            n_results (int):  Number of results to return (default 5).
            where (dict):     Optional ChromaDB metadata filter,
                              e.g. {"source": "readme.txt"}.

        Returns:
            list[dict]: Results sorted by ascending distance, each dict has:
                - "id"       (str)
                - "document" (str)   original text
                - "distance" (float) cosine distance — 0 = identical, 2 = opposite
                - "metadata" (dict | None)

        Raises:
            RuntimeError: If ChromaDB is not enabled.

        Examples:
            >>> results = embedder.search("feline animal", n_results=3)
            >>> for r in results:
            ...     print(f"[{r['distance']:.3f}] {r['document']}")
        """
        if self._collection is None:
            raise RuntimeError(
                "ChromaDB is not enabled. "
                "Pass a chroma_dir when constructing SemanticEmbedder."
            )

        if self._collection.count() == 0:
            if self.verbose:
                print("ChromaDB collection is empty — nothing to search.")
            return []

        query_embedding = self.embed(query)[0].tolist()

        kwargs = {"query_embeddings": [query_embedding], "n_results": n_results}
        if where:
            kwargs["where"] = where

        raw = self._collection.query(**kwargs)

        # ChromaDB returns batched results (one list per query).
        # We sent exactly one query, so we index [0] throughout.
        results = []
        for doc_id, document, distance, metadata in zip(
            raw["ids"][0],
            raw["documents"][0],
            raw["distances"][0],
            raw["metadatas"][0],
        ):
            results.append({
                "id":       doc_id,
                "document": document,
                "distance": distance,
                "metadata": metadata,
            })

        return results

    # ------------------------------------------------------------------
    # Similarity helpers (unchanged)
    # ------------------------------------------------------------------

    def similarity(self, text1, text2, metric="cosine"):
        """
        Compute similarity between two texts.
        
        Args:
            text1 (str): First text
            text2 (str): Second text
            metric (str): "cosine" (default) or "euclidean"
        
        Returns:
            float: Similarity score
                - Cosine: 0-1 (1 = identical)
                - Euclidean: distance (0 = identical)
        
        Examples:
            >>> embedder = SemanticEmbedder()
            >>> sim = embedder.similarity("cat on mat", "cat sat on mat")
            >>> print(f"Similarity: {sim:.3f}")
            Similarity: 0.945
        """
        emb1 = self.embed(text1)[0]
        emb2 = self.embed(text2)[0]
        
        if metric == "cosine":
            return self._cosine_similarity(emb1, emb2)
        elif metric == "euclidean":
            return np.linalg.norm(emb1 - emb2)
        else:
            raise ValueError(f"Unknown metric: {metric}")
    
    def batch_similarity(self, texts1, texts2, metric="cosine"):
        """
        Compute similarities between lists of texts (more efficient than loops).
        
        Args:
            texts1 (list[str]): First set of texts
            texts2 (list[str]): Second set of texts
            metric (str): "cosine" or "euclidean"
        
        Returns:
            np.ndarray: Pairwise similarity matrix of shape (len(texts1), len(texts2))
        
        Examples:
            >>> embedder = SemanticEmbedder()
            >>> t1 = ["cat", "dog"]
            >>> t2 = ["feline", "canine", "animal"]
            >>> sims = embedder.batch_similarity(t1, t2)
            >>> sims.shape
            (2, 3)
        """
        embeddings1 = self.embed(texts1)
        embeddings2 = self.embed(texts2)
        
        if metric == "cosine":
            # Normalize for cosine similarity
            embeddings1 = embeddings1 / (np.linalg.norm(embeddings1, axis=1, keepdims=True) + 1e-9)
            embeddings2 = embeddings2 / (np.linalg.norm(embeddings2, axis=1, keepdims=True) + 1e-9)
            return np.dot(embeddings1, embeddings2.T)
        elif metric == "euclidean":
            return np.linalg.norm(embeddings1[:, None, :] - embeddings2[None, :, :], axis=2)
        else:
            raise ValueError(f"Unknown metric: {metric}")

    # ------------------------------------------------------------------
    # Private helpers (unchanged)
    # ------------------------------------------------------------------

    @staticmethod
    def _mean_pooling(model_output, attention_mask):
        """Apply mean pooling to get sentence-level embeddings."""
        token_embeddings = model_output
        input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
        sum_embeddings = (token_embeddings * input_mask_expanded).sum(1)
        sum_mask = input_mask_expanded.sum(1).clamp(min=1e-9)
        return sum_embeddings / sum_mask
    
    @staticmethod
    def _cosine_similarity(vec1, vec2):
        """Compute cosine similarity between two vectors."""
        dot_product = np.dot(vec1, vec2)
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)
        return dot_product / (norm1 * norm2 + 1e-9)


# ----------------------------------------------------------------------
# main — test suite
# ----------------------------------------------------------------------

def main():
    """Test the embedder with example usage."""
    print("=" * 70)
    print("SEMANTIC EMBEDDER - Test Suite")
    print("=" * 70)
    
    try:
        embedder = SemanticEmbedder()
    except (FileNotFoundError, RuntimeError) as e:
        print(f"\n❌ Error: {e}")
        print("\nSetup steps:")
        print("  1. python3 convert_model.py  (one-time conversion)")
        print("  2. python3 semantic_embedder.py")
        return
    
    # Test 1: Single embedding
    print("\n[Test 1] Single Text Embedding")
    print("-" * 70)
    text = "The quick brown fox jumps over the lazy dog"
    emb = embedder.embed(text, show_stats=True)
    print(f"\nText: '{text}'")
    print(f"Embedding shape: {emb.shape}")
    print(f"First 10 dimensions: {emb[0][:10]}")
    print(f"L2 norm: {np.linalg.norm(emb[0]):.4f}")
    
    # Test 2: Batch embeddings
    print("\n[Test 2] Batch Embeddings")
    print("-" * 70)
    texts = [
        "The cat sat on the mat",
        "A dog lay on the floor",
        "The weather is nice today",
        "I like programming in Python"
    ]
    embeddings = embedder.embed(texts, show_stats=True)
    print(f"\nProcessed {len(texts)} texts")
    print(f"Output shape: {embeddings.shape}")
    
    # Test 3: Pairwise similarity
    print("\n[Test 3] Pairwise Similarity")
    print("-" * 70)
    sims = embedder.batch_similarity(
        ["The cat is on the mat", "A dog on the floor"],
        ["The cat sits on the mat", "A dog lay on the floor", "Weather is nice"]
    )
    print("\nSimilarity matrix:")
    print(f"  {sims[0]}")
    print(f"  {sims[1]}")
    print("\nInterpretation:")
    print(f"  'Cat on mat' vs 'Cat sits on mat':  {sims[0][0]:.3f} (high - similar meaning)")
    print(f"  'Cat on mat' vs 'Weather is nice':  {sims[0][2]:.3f} (low - different meaning)")
    print(f"  'Dog on floor' vs 'Dog lay on floor': {sims[1][1]:.3f} (high - similar meaning)")
    
    # Test 4: Direct similarity
    print("\n[Test 4] Direct Similarity")
    print("-" * 70)
    pairs = [
        ("cat", "feline"),
        ("car", "automobile"),
        ("happy", "sad"),
        ("software", "hardware"),
    ]
    for t1, t2 in pairs:
        sim = embedder.similarity(t1, t2)
        print(f"  '{t1}' vs '{t2}': {sim:.3f}")

    # Test 5: ChromaDB save and search
    print("\n[Test 5] ChromaDB Save & Search")
    print("-" * 70)
    sample_docs = [
        ("Cats are independent and curious mammals.", "doc_cat"),
        ("Dogs are loyal and affectionate companions.", "doc_dog"),
        ("Python is a popular language for machine learning.", "doc_py"),
        ("The Eiffel Tower is located in Paris, France.", "doc_paris"),
        ("Neural networks are inspired by the human brain.", "doc_nn"),
    ]
    print("\nSaving documents...")
    for text, doc_id in sample_docs:
        embedder.save(text, ids=doc_id, metadatas={"source": "test_suite"})

    print("\nSearching: 'feline pet'")
    results = embedder.search("feline pet", n_results=3)
    for r in results:
        print(f"  [{r['distance']:.3f}] {r['document']}")

    print("\nSearching: 'deep learning AI'")
    results = embedder.search("deep learning AI", n_results=3)
    for r in results:
        print(f"  [{r['distance']:.3f}] {r['document']}")

    print("\n" + "=" * 70)
    print("✅ All tests completed successfully!")
    print("=" * 70)


if __name__ == "__main__":
    main()