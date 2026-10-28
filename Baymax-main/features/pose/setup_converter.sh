#!/usr/bin/env bash
# Create the private Python environments that convert_rknn.py uses (run on the Orange Pi).
#
#   features/pose/setup_converter.sh [--rknn-version 2.3.2|2.5.0]
#
#   <model dir>/build/venv-onnx   tensorflow + tf2onnx: .task TFLite -> ONNX
#   <model dir>/build/venv-rknn   rknn-toolkit2 (+ rknn-toolkit-lite2): ONNX -> RKNN, NPU check
#
# The model dir is Baymax-main/.models/pose_npu (or BAYMAX_POSE_MODEL_DIR). Needs
# Python 3.12 (the app's .venv by default, or PYTHON=/path/to/python3.12), internet
# access to PyPI, and about 3 GB of disk. Nothing here needs sudo or touches the
# app's own .venv. Use the same --rknn-version as the rknn-toolkit-lite2 and
# librknnrt.so the app uses (see ORANGE_PI_SETUP.md).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
RKNN_VERSION="2.3.2"
while [ $# -gt 0 ]; do
  case "$1" in
    --rknn-version) RKNN_VERSION="$2"; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
case "$RKNN_VERSION" in
  2.3.2|2.5.0) ;;
  *) echo "Tested toolkit versions are 2.3.2 and 2.5.0 (got $RKNN_VERSION)." >&2; exit 2 ;;
esac

MODEL_DIR="${BAYMAX_POSE_MODEL_DIR:-$ROOT/.models/pose_npu}"
BUILD="$MODEL_DIR/build"
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then PYTHON="$ROOT/.venv/bin/python"; else PYTHON="$(command -v python3.12 || true)"; fi
fi
if [ -z "$PYTHON" ]; then
  echo "Python 3.12 not found: run Setup first, or set PYTHON=/path/to/python3.12." >&2; exit 1
fi
VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if [ "$VERSION" != "3.12" ]; then
  echo "$PYTHON is Python $VERSION; the rknn-toolkit2 wheels used here need 3.12." >&2; exit 1
fi
if [ "$(uname -s)" != "Linux" ]; then
  echo "rknn-toolkit2 is Linux-only (aarch64 on the Orange Pi, or x86_64)." >&2; exit 1
fi
[ "$(uname -m)" = "aarch64" ] || echo "Note: $(uname -m) can convert, but the NPU check only runs on the Orange Pi."

mkdir -p "$BUILD"
make_venv() {
  echo "== $1"
  "$PYTHON" -m venv --clear "$1"
  "$1/bin/python" -m pip install --quiet --upgrade pip
}

make_venv "$BUILD/venv-onnx"
"$BUILD/venv-onnx/bin/python" -m pip install \
  "tensorflow==2.21.0" "tf2onnx==1.17.0" "onnx==1.23.2" "onnxruntime==1.31.1" \
  "ai-edge-litert==2.3.0" "opencv-python-headless==4.10.0.84"

make_venv "$BUILD/venv-rknn"
# rknn-toolkit2 (2.3.2 and 2.5.0) imports pkg_resources, which a fresh Python 3.12
# venv no longer has (setuptools isn't preinstalled): found on the Orange Pi 5 Max.
"$BUILD/venv-rknn/bin/python" -m pip install \
  "rknn-toolkit2==$RKNN_VERSION" "rknn-toolkit-lite2==$RKNN_VERSION" "setuptools<80.1"

echo
echo "Converter environments ready in $BUILD (rknn-toolkit2 $RKNN_VERSION)."
echo "Next: $PYTHON $HERE/convert_rknn.py"
