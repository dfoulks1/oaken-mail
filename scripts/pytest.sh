#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Run the test suite.
#
#   scripts/pytest.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

uv run scripts/run_check.py .pytest.lastrun "pytest" -- \
    "sync"    uv sync --quiet  -- \
    "pytest"  uv run pytest -q
