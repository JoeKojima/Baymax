#!/usr/bin/env python3
"""
Convert sentence-transformers/all-MiniLM-L6-v2 to ONNX format
Optimized for Intel CPU (LattePanda)

Run once: python3 convert_model.py
"""

import os
import sys
from pathlib import Path

def main():
    print("=" * 60)
    print("ONNX Model Conversion for all-MiniLM-L6-v2")
    print("=" * 60)
    
    try:
        from optimum.onnxruntime import ORTModelForFeatureExtraction
        from transformers import AutoTokenizer
    except ImportError as e:
        print(f"\n❌ Missing dependency: {e}")
        print("\nInstall with:")
        print("  pip install optimum[onnxruntime] transformers")
        sys.exit(1)
    
    model_name = "sentence-transformers/all-MiniLM-L6-v2"
    output_dir = "./all-MiniLM-L6-v2-onnx"
    
    print(f"\nModel: {model_name}")
    print(f"Output: {output_dir}")
    print(f"Target: Intel CPU (ONNX Runtime)")
    
    # Check if already exists
    if Path(output_dir).exists():
        print(f"\n⚠️  Directory '{output_dir}' already exists.")
        response = input("Overwrite? (y/n): ").strip().lower()
        if response != 'y':
            print("Cancelled.")
            return
    
    print("\n[1/3] Downloading model from HuggingFace...")
    print("      (This may take 1-2 minutes on first run)")
    
    try:
        # New optimum API: use export=True instead of from_transformers=True
        ort_model = ORTModelForFeatureExtraction.from_pretrained(
            model_name,
            export=True
        )
        print("      ✓ Model downloaded and converted to ONNX")
    except Exception as e:
        print(f"\n❌ Conversion failed: {e}")
        print("\nTroubleshooting:")
        print("  1. Check internet connection")
        print("  2. Try: pip install --upgrade optimum transformers")
        print("  3. Free up disk space (need ~500MB)")
        sys.exit(1)
    
    print("\n[2/3] Loading tokenizer...")
    try:
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        print("      ✓ Tokenizer loaded")
    except Exception as e:
        print(f"\n❌ Tokenizer loading failed: {e}")
        sys.exit(1)
    
    print(f"\n[3/3] Saving to {output_dir}...")
    try:
        ort_model.save_pretrained(output_dir)
        tokenizer.save_pretrained(output_dir)
        print("      ✓ Model and tokenizer saved")
    except Exception as e:
        print(f"\n❌ Save failed: {e}")
        sys.exit(1)
    
    # Verify files
    output_path = Path(output_dir)
    files = list(output_path.glob("*"))
    
    print("\n" + "=" * 60)
    print("✅ CONVERSION SUCCESSFUL")
    print("=" * 60)
    print(f"\nFiles created ({len(files)} total):")
    for f in sorted(files):
        size_mb = f.stat().st_size / (1024*1024) if f.is_file() else 0
        if f.is_file():
            print(f"  • {f.name:<30} {size_mb:>6.1f} MB")
    
    print(f"\n📊 Total size: {sum(f.stat().st_size for f in files if f.is_file()) / (1024*1024):.1f} MB")
    print("\n✨ You can now use this model with semantic_embedder.py!")
    print(f"   import semantic_embedder")
    print(f"   embedder = semantic_embedder.SemanticEmbedder('{output_dir}')")
    
if __name__ == "__main__":
    main()