# SPDX-License-Identifier: Apache-2.0
"""The software map must reject data that contradicts the repository or the layering."""

from pathlib import Path

import build_site
import pytest
from model import BuildError

from tests.docs.conftest import Tree

MAP = """\
[[layers]]
id = "top"
name = "Top"

[[layers]]
id = "bottom"
name = "Bottom"

[[components]]
id = "a"
name = "Alpha"
layer = "top"
status = "built"
prompt = "01"
path = "src/x.txt"
summary = "The top piece."
uses = ["b"]

[[components]]
id = "b"
name = "Beta | pipe"
layer = "bottom"
status = "planned"
prompt = "02"
summary = "The bottom piece."
uses = []
"""


def with_map(tree: Tree, text: str = MAP) -> Tree:
    tree.write("docs/softmap.toml", text)
    tree.write(
        "docs/site.toml",
        (tree.docs / "site.toml").read_text()
        + '\n[[pages]]\nid = "map"\ngenerated = true\ngroup = "g1"\n',
    )
    import generated

    if "map" not in generated._REGISTRY:  # register the real builder under the fixture's id
        generated.register("map", generated._REGISTRY["architecture/software-map"])
    return tree


def test_map_renders_counts_status_and_diagram(tree: Tree, tmp_path: Path) -> None:
    with_map(tree)
    out = tmp_path / "out"
    build_site.build(tree.cfg(), out)
    html = (out / "map.html").read_text(encoding="utf-8")
    assert "1 built" in html
    assert "1 planned" in html
    assert "badge text-bg-success" in html
    assert "flowchart TB" in html
    assert "a ~~~ b" in html
    assert "Beta &#124; pipe" in html or "Beta | pipe" in html
    assert "blob/main/src/x.txt" in html
    assert "mermaid@" in html


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        ('uses = ["b"]', 'uses = ["zzz"]', "uses unknown component"),
        (
            'layer = "bottom"\nstatus = "planned"',
            'layer = "nowhere"\nstatus = "planned"',
            "unknown layer",
        ),
        ('status = "planned"', 'status = "done"', "status must be one of"),
        ('path = "src/x.txt"', 'path = "src/missing.txt"', "does not exist"),
        (
            'summary = "The bottom piece."',
            'summary = "The bottom piece."\npath = "src/x.txt"',
            "marked planned",
        ),
        ('id = "b"', 'id = "a"', "duplicate component id"),
        ('summary = "The top piece."\n', "", "missing 'summary'"),
    ],
)
def test_map_validation_failures(
    tree: Tree, tmp_path: Path, old: str, new: str, expect: str
) -> None:
    with_map(tree, MAP.replace(old, new, 1))
    with pytest.raises(BuildError, match=expect):
        build_site.build(tree.cfg(), tmp_path / "out")


def test_calls_may_not_point_up(tree: Tree, tmp_path: Path) -> None:
    bad = MAP.replace("uses = []", 'uses = ["a"]', 1)
    with_map(tree, bad)
    with pytest.raises(BuildError, match="layering: b calls up into a"):
        build_site.build(tree.cfg(), tmp_path / "out")
