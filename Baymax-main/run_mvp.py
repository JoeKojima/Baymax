"""Stable entry point for Ember; startup implementation lives in features/startup."""
import sys
from features.startup import launcher
if __name__ == "__main__":
    sys.exit(launcher.main())
else:
    sys.modules[__name__] = launcher
