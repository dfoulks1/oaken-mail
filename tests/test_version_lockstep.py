# SPDX-License-Identifier: Apache-2.0
"""Oaken Core is one product assembled from four distributions (ADR 0014); they release in
lockstep. There is **one** version — ``oaken.__version__`` — and the three apps read it from there
(dynamic hatch version), so they cannot drift. These tests pin that arrangement in place."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = "../../src/oaken/__init__.py"  # the one source of truth, relative to each app


def _oaken_version() -> str:
    text = (ROOT / "src" / "oaken" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise AssertionError("oaken.__version__ not found")


APPS = ("apps/cli/pyproject.toml", "apps/core/pyproject.toml")


def test_the_apps_source_their_version_from_the_one_place() -> None:
    """Each app has a dynamic version read from oaken/__init__.py, not a literal of its own — so a
    single bump of oaken.__version__ moves the whole product and nothing is hand-synced."""
    for rel in APPS:
        data = tomllib.loads((ROOT / rel).read_text(encoding="utf-8"))
        assert "version" in data["project"].get("dynamic", []), f"{rel}: version must be dynamic"
        hatch = data["tool"]["hatch"]["version"]
        assert hatch["source"] == "regex" and hatch["path"] == SHARED, (
            f"{rel}: version must be sourced from {SHARED}"
        )


def test_the_core_bundle_pins_the_shared_version() -> None:
    """oaken-core must pin its members to the exact shared version — the one spot that restates it
    (a dependency string cannot be dynamic), so it is asserted here against the source of truth."""
    version = _oaken_version()
    data = tomllib.loads((ROOT / "apps/core/pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    assert f"oaken[vec]=={version}" in deps, deps
    assert f"oaken-cli=={version}" in deps, deps
