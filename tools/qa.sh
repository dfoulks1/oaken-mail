#!/usr/bin/env bash
# The whole QA run: ruff, mypy, pytest, each to a one-line answer. Exits non-zero if any failed. Full output lands in .qa/.
set -u
cd "$(dirname "$0")/.."
status=0
for check in ruff mypy pytest; do
  tools/$check.sh || status=1
done
exit $status
