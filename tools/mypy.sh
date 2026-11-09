#!/usr/bin/env bash
# Types: mypy over src. Extra arguments go to mypy.
set -u
cd "$(dirname "$0")/.."
exec python3 tools/run_check.py mypy -- uv run --quiet --with mypy mypy --follow-untyped-imports src "$@"
