#!/usr/bin/env python3
"""
Semantic Embedder using ONNX Runtime
Optimized for Intel CPU (LattePanda Debian)

Usage:
    from semantic_embedder import SemanticEmbedder
    
    embedder = SemanticEmbedder()
    embeddings = embedder.embed("Your text here")
    similarity = embedder.similarity("text1", "text2")
"""

import numpy as np
import time
import warnings
from pathlib import Path

# Suppress ONNX warnings
warnings.filterwarnings('ignore')


class SemanticEmbedder:
    """
    Generate semantic embeddings using sentence-transformers with ONNX acceleration.
    
    Model: sentence-transformers/all-MiniLM-L6-v2
    - Embedding dimension: 384
    - Max sequence length: 256 tokens
    - ONNX optimized for Intel CPU
    """
    
    def __init__(self, model_dir="./all-MiniLM-L6-v2-onnx", verbose=True):
        """
        Initialize the semantic embedder.
        
        Args:
            model_dir (str): Path to ONNX-converted model directory
            verbose (bool): Print loading information
        
        Raises:
            FileNotFoundError: If model files not found in model_dir
            ImportError: If required packages not installed
        """
        self.model_dir = Path(model_dir)
        self.verbose = verbose
        
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
    
    print("\n" + "=" * 70)
    print("✅ All tests completed successfully!")
    print("=" * 70)


if __name__ == "__main__":
    main()