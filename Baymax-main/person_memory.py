"""Compatibility import; implementation lives in the feature group."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("features.memory_palace.person_memory")
