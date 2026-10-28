#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Run ruff lint and format checks.
#
#   scripts/ruff.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

uv run scripts/run_check.py .ruff.lastrun "ruff" -- \
    "sync"              uv sync --quiet         -- \
    "ruff check"        uv run ruff check .     -- \
    "ruff format"       uv run ruff format --check .
