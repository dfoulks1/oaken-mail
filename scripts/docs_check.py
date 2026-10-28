#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Build the docs site and run all doc checks.

    uv run scripts/docs_check.py

Steps:
  sync              uv sync --quiet
  build the site    uv run python docs/tools/build_site.py
  check docs        uv run python docs/tools/check_docs.py
  check commands    uv run python docs/tools/check_commands.py
  check prompts     uv run python docs/tools/check_prompts.py
  TODO markers      scan *.md files for TODO(user) outside docs/tools/
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.chdir(Path(__file__).parent.parent)
sys.path.insert(0, str(Path(__file__).parent))

from run_check import run  # noqa: E402


def _todo_cmd() -> list[str]:
    script = (
        "import subprocess, sys; "
        "r = subprocess.run("
        "    ['grep', '-rn', 'TODO(user)', '--include=*.md', '.', "
        "     '--exclude-dir=.venv', '--exclude-dir=.git'], "
        "    capture_output=True, text=True"
        "); "
        "lines = [l for l in r.stdout.splitlines()"
        "         if not l.startswith('./docs/tools/')]; "
        "print('\\n'.join(lines) if lines else 'none')"
    )
    return ["uv", "run", "python", "-c", script]


STEPS: list[tuple[str, list[str]]] = [
    ("sync",               ["uv", "sync", "--quiet"]),
    ("build the site",     ["uv", "run", "python", "docs/tools/build_site.py"]),
    ("check docs",         ["uv", "run", "python", "docs/tools/check_docs.py"]),
    ("check commands",     ["uv", "run", "python", "docs/tools/check_commands.py"]),
    ("check prompts",      ["uv", "run", "python", "docs/tools/check_prompts.py"]),
    ("TODO(user) markers", _todo_cmd()),
]

if __name__ == "__main__":
    run(Path(__file__).parent / ".docs_check.lastrun", "docs", STEPS)
