# SPDX-License-Identifier: Apache-2.0
"""The import boundaries (ADR 0002, 0006, 0007, 0010) hold.

Runs import-linter over the configured contracts and fails if any is broken, so a boundary
violation shows up in ``pytest`` as well as in CI and pre-commit. Invokes the same
``lint-imports`` entry point CI runs, from the project root where ``[tool.importlinter]`` lives.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _lint_imports_cmd() -> list[str]:
    """The ``lint-imports`` console script if it is on PATH, else the module entry point."""
    script = shutil.which("lint-imports")
    if script:
        return [script]
    return [sys.executable, "-m", "importlinter"]


def test_import_boundaries_hold() -> None:
    try:
        result = subprocess.run(  # noqa: S603 (fixed args, our own tooling)
            _lint_imports_cmd(),
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:  # pragma: no cover - import-linter should be in the dev env
        pytest.skip("import-linter is not installed")
    assert result.returncode == 0, (
        "import-linter found a broken boundary (ADR 0002/0006/0007/0010). "
        "Run `uv run lint-imports` to see which contract and import broke.\n\n"
        + result.stdout
        + result.stderr
    )
