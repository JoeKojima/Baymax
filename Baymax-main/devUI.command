#!/bin/sh
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo 'Follow README.md to create the Python environment first.'
  exit 1
fi
exec .venv/bin/python devUI.py
