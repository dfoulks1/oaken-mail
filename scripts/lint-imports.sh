#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Run import-linter boundary checks.
#
#   scripts/lint-imports.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

uv run scripts/run_check.py .lint_imports.lastrun "lint-imports" -- \
    "sync"          uv sync --quiet  -- \
    "lint-imports"  uv run lint-imports
