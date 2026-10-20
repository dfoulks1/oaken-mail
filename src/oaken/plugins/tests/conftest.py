# SPDX-License-Identifier: Apache-2.0
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

Installer = Callable[[str, str, list[tuple[str, str, str]]], None]


@pytest.fixture
def install_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Installer:
    """Install a throwaway distribution: a module plus a real ``.dist-info`` with entry points,
    found through ``importlib.metadata`` exactly as an installed package would be.

    ``install(module, source, [("oaken.embedders", "name", "Attr"), ...])``
    """
    root = tmp_path / "site"
    root.mkdir()
    monkeypatch.syspath_prepend(str(root))

    def install(module: str, source: str, points: list[tuple[str, str, str]]) -> None:
        (root / f"{module}.py").write_text(textwrap.dedent(source))
        dist = root / f"{module}-1.2.3.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {module}\nVersion: 1.2.3\n")
        by_group: dict[str, list[str]] = {}
        for group, name, attr in points:
            by_group.setdefault(group, []).append(f"{name} = {module}:{attr}")
        lines = [f"[{g}]\n" + "\n".join(items) + "\n" for g, items in by_group.items()]
        (dist / "entry_points.txt").write_text("\n".join(lines))
        monkeypatch.delitem(sys.modules, module, raising=False)

    return install
