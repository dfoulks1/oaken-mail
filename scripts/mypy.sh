#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Run mypy type checks.
#
#   scripts/mypy.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

uv run scripts/run_check.py .mypy.lastrun "mypy" -- \
    "sync"   uv sync --quiet  -- \
    "mypy"   uv run mypy .
