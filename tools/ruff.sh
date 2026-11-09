#!/usr/bin/env bash
# Lint: ruff's default rules over src and tests. Extra arguments go to ruff. (No ruff config in pyproject yet, and no format check.)
set -u
cd "$(dirname "$0")/.."
exec python3 tools/run_check.py ruff -- uv run --quiet --with ruff ruff check src tests "$@"
