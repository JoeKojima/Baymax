import importlib
import sys
sys.modules[__name__] = importlib.import_module("features.pose_detection.fall_detection.detector")
