"""Compatibility import; implementation lives in the feature group."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("features.pose_detection.worker")
