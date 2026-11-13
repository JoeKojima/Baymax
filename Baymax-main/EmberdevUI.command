#!/bin/bash
cd "$(dirname "$0")"
exec .venv/bin/python devUI.py --monitor
