# SPDX-License-Identifier: Apache-2.0
"""Fixtures for the docs tooling tests: a throwaway content tree, never the real docs."""

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "docs" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from model import Config, load_config  # noqa: E402

SITE_TOML = """\
exclude = ["private/**"]

[site]
title = "Fixture"
repo = "https://github.com/example/fixture"
url = "https://example.github.io/fixture/"
branch = "main"
home = "index.md"

[[groups]]
id = "g1"
name = "First group"

[[groups]]
id = "g2"
name = "Second group"

[[pages]]
id = "alpha"
file = "alpha.md"

[[pages]]
id = "deep/beta"
file = "deep/beta.md"
"""

INDEX = """\
+++
title = "Fixture home"
lead = "Home lead."
badges = [["text-bg-warning", "Pre-alpha"]]
+++

## About

Some text.

## Docs

{{doc-cards}}
"""

ALPHA = """\
+++
title = "Alpha"
subtitle = "The first page"
lead = "Alpha lead."
group = "g1"
order = 1
description = "Alpha blurb."
+++

Intro text.

## One

First section, linking [beta](deep/beta.md) and [a file](repo:src/x.txt) and [dir](repo:src).

### Sub

```python
print("hi")
```

::: callout
A callout.
:::

## Two

| a | b |
|---|---|
| 1 | 2 |

```mermaid
flowchart TD
    A --> B
```
"""

BETA = """\
+++
title = "Beta"
group = "g2"
order = 1
+++

## Only

Back to [alpha two](../alpha.md#two).
"""


class Tree:
    """A temporary docs tree with a repo root one level up."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.docs = root / "docs"
        self.write("docs/site.toml", SITE_TOML)
        self.write("docs/index.md", INDEX)
        self.write("docs/alpha.md", ALPHA)
        self.write("docs/deep/beta.md", BETA)
        self.write("src/x.txt", "x\n")

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def append(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")

    def cfg(self) -> Config:
        return load_config(self.docs, self.root)


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return Tree(tmp_path / "repo")


@pytest.fixture
def built(tree: Tree, tmp_path: Path) -> Callable[[], Path]:
    """Build the fixture tree and return the output directory."""
    import build_site

    def _build() -> Path:
        out = tmp_path / "out"
        build_site.build(tree.cfg(), out)
        return out

    return _build
