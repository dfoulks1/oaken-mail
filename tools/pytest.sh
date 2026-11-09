#!/usr/bin/env bash
# Tests: pytest as CI runs it (quiet, stop-on-nothing). Extra arguments go to pytest, e.g. tools/pytest.sh tests/test_webui.py -k proxy
set -u
cd "$(dirname "$0")/.."
exec python3 tools/run_check.py pytest -- uv run --quiet pytest -q "$@"
