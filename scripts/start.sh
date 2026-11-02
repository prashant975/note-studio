#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  "${PYTHON:-python3.13}" scripts/setup.py
fi
exec .venv/bin/python run_studio.py "$@"
