#!/usr/bin/env python3
"""
Semantic Embedder using ONNX Runtime
Runs MiniLM with onnxruntime and the `tokenizers` library only (no torch or
transformers), so it also fits the Orange Pi and other ARM boards.

Usage:
    from semantic_embedder import SemanticEmbedder
    
    embedder = SemanticEmbedder()
    embeddings = embedder.embed("Your text here")
    similarity = embedder.similarity("text1", "text2")
    
    # Save to ChromaDB
    embedder.save("Some text", ids="doc_1", metadatas={"source": "manual"})
    
    # Search ChromaDB
    results = embedder.search("query text", n_results=5)

Model: a directory with model.onnx and tokenizer.json, either built by
convert_model.py (needs optimum and torch) or fetched ready-made with
`python semantic_embedder.py --download`.
"""

import hashlib
import importlib.util
import os
import sys
import tempfile
import urllib.request
import uuid
import numpy as np
import time
import warnings
from pathlib import Path

# Suppress ONNX warnings
warnings.filterwarnings('ignore')

# The model's own ONNX export on Hugging Face, pinned to one revision and checked
# by size and SHA-256. Same network and inputs/outputs as convert_model.py's export.
MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
MODEL_URL = ("https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/"
             "resolve/{revision}/{path}")
# Written in this order; model.onnx last, so it only exists once the download is complete.
MODEL_FILES = {
    "tokenizer.json": ("tokenizer.json", 466247,
                       "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037"),
    "model.onnx": ("onnx/model.onnx", 90405214,
                   "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452"),
}
REQUIRED_PACKAGES = ("onnxruntime", "tokenizers", "chromadb")
# A partial download older than this was left by a power cut or a killed process; one
# still being written is newer (a stalled read gives up after `timeout` seconds).
STALE_AFTER_S = 600
MAX_TOKENS = 256
# Two threads are plenty for a sentence. An explicit count also stops onnxruntime
# pinning one thread per core itself, which on the Orange Pi would put work on the
# slow cores Ember was moved off (pc_hardware.use_performance_cpus).
THREADS = min(2, os.cpu_count() or 1)


def missing_packages():
    """Packages memory needs that aren't installed (checked without importing them)."""
    return [name for name in REQUIRED_PACKAGES if importlib.util.find_spec(name) is None]


def model_present(model_dir):
    return all((Path(model_dir) / name).is_file() for name in MODEL_FILES)


def download_model(model_dir, timeout=60):
    """Fetch the pinned model.onnx and tokenizer.json (about 90 MB) into model_dir.
    Each file must match its size and SHA-256 before it is moved into place."""
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, (path, size, sha256) in MODEL_FILES.items():
        for stale in model_dir.glob(f".{name}.*"):
            try:
                if time.time() - stale.stat().st_mtime > STALE_AFTER_S:
                    stale.unlink()
            except OSError:
                pass
        url = MODEL_URL.format(revision=MODEL_REVISION, path=path)
        tmp = tempfile.NamedTemporaryFile(dir=model_dir, prefix=f".{name}.", delete=False)
        try:
            digest, received = hashlib.sha256(), 0
            with tmp, urllib.request.urlopen(url, timeout=timeout) as response:
                while received <= size and (chunk := response.read(1 << 20)):
                    digest.update(chunk)
                    tmp.write(chunk)
                    received += len(chunk)
                if received != size or digest.hexdigest() != sha256:
                    raise RuntimeError(f"{name} from {url} failed its check "
                                       f"({received} bytes, sha256 {digest.hexdigest()})")
                tmp.flush()
                os.fsync(tmp.fileno())  # on disk before the rename, so a power cut can't leave it empty
            os.chmod(tmp.name, 0o644)  # mkstemp makes files readable by their owner only
            os.replace(tmp.name, model_dir / name)
        except BaseException:
            Path(tmp.name).unlink(missing_ok=True)
            raise


class SemanticEmbedder:
    """
    Generate semantic embeddings using sentence-transformers with ONNX acceleration.
    Optionally persists embeddings to a local ChromaDB store.

    Model: sentence-transformers/all-MiniLM-L6-v2
    - Embedding dimension: 384
    - Max sequence length: 256 tokens
    - ONNX Runtime on the CPU
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
                "Run: python3 semantic_embedder.py --download"
            )
        
        if not model_present(self.model_dir):
            raise FileNotFoundError(
                f"model.onnx or tokenizer.json not found in {model_dir}\n"
                "Model directory is incomplete or corrupted."
            )
        
        # Import here to fail gracefully
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as e:
            raise ImportError(
                f"Missing package: {e}\n"
                "Install with: pip install onnxruntime tokenizers"
            )
        
        if self.verbose:
            print("Loading ONNX model...")
        
        start_time = time.time()
        
        try:
            # The settings transformers' tokenizer used here: lower-cased WordPiece,
            # cut at 256 tokens, a batch padded to its longest text.
            self.tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
            self.tokenizer.enable_truncation(max_length=MAX_TOKENS)
            self.tokenizer.enable_padding(pad_id=self.tokenizer.token_to_id("[PAD]"),
                                          pad_token="[PAD]")
            options = ort.SessionOptions()
            options.intra_op_num_threads = THREADS
            options.inter_op_num_threads = 1
            # Threads sleep between calls instead of spinning on the CPU the audio uses.
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
            self.model = ort.InferenceSession(str(self.model_dir / "model.onnx"), options,
                                              providers=["CPUExecutionProvider"])
            self._inputs = {i.name for i in self.model.get_inputs()}
        except Exception as e:
            raise RuntimeError(
                f"Failed to load model: {e}\n"
                "Model files may be corrupted. Delete the directory and run: "
                "python3 semantic_embedder.py --download"
            )
        
        elapsed = time.time() - start_time
        
        if self.verbose:
            print(f"✓ Model loaded in {elapsed:.2f}s")
            print(f"  - Embedding dimension: 384")
            print(f"  - Max sequence: 256 tokens")
            print(f"  - ONNX Runtime, {THREADS} threads")

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
        encodings = self.tokenizer.encode_batch(texts)
        encoded = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encodings], dtype=np.int64),
        }
        
        tokenize_time = time.time() - start_time
        
        # Run inference
        inference_start = time.time()
        last_hidden_state = self.model.run(
            ["last_hidden_state"],
            {name: value for name, value in encoded.items() if name in self._inputs}
        )[0]
        inference_time = time.time() - inference_start
        
        # Mean pooling to get sentence embeddings
        pool_start = time.time()
        embeddings_np = self._mean_pooling(
            last_hidden_state,
            encoded['attention_mask']
        )
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
        input_mask_expanded = np.broadcast_to(
            attention_mask[..., None], token_embeddings.shape).astype(np.float32)
        sum_embeddings = (token_embeddings * input_mask_expanded).sum(1)
        sum_mask = np.clip(input_mask_expanded.sum(1), 1e-9, None)
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
        print("  1. python3 semantic_embedder.py --download  (or python3 convert_model.py)")
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
    if sys.argv[1:] == ["--download"]:
        # Where realtime_gemini_10.py looks for it.
        target = Path(__file__).resolve().parent / "all-MiniLM-L6-v2-onnx"
        if model_present(target):
            print(f"Model already in {target}")
        else:
            print(f"Downloading all-MiniLM-L6-v2 (about 90 MB) to {target} ...")
            download_model(target)
            print("✓ Downloaded and checked")
    else:
        main()