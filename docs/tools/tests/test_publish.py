# SPDX-License-Identifier: Apache-2.0
"""Publishing writes the site into docs/ beside the sources, safely, and staleness is caught."""

import build_site
import check_docs
from treekit import Tree


def publish(tree: Tree) -> list[str]:
    return build_site.build(tree.cfg(), tree.docs)


def test_publish_writes_html_next_to_sources_and_keeps_sources(tree: Tree) -> None:
    files = publish(tree)
    assert (tree.docs / "index.html").is_file() and (tree.docs / "deep" / "beta.html").is_file()
    assert (tree.docs / ".nojekyll").is_file()
    for src in ("site.toml", "index.md", "alpha.md", "deep/beta.md"):
        assert (tree.docs / src).is_file()
    manifest = (tree.docs / ".site-manifest").read_text().splitlines()
    assert manifest == sorted(manifest) and set(manifest) <= set(files)
    assert "index.md" not in manifest and "site.toml" not in manifest


def test_republish_is_stable_and_never_touches_unlisted_files(tree: Tree) -> None:
    publish(tree)
    first = {p: p.read_bytes() for p in tree.docs.rglob("*.html")}
    mine = tree.write("docs/notes.txt", "mine")
    publish(tree)
    assert {p: p.read_bytes() for p in tree.docs.rglob("*.html")} == first
    assert mine.read_text() == "mine"


def test_files_no_longer_generated_are_removed(tree: Tree) -> None:
    publish(tree)
    old = tree.docs / "old" / "page.html"
    old.parent.mkdir()
    old.write_text("x")
    manifest = tree.docs / ".site-manifest"
    manifest.write_text(manifest.read_text() + "old/page.html\n")
    publish(tree)
    assert not old.exists() and not old.parent.exists()
    assert (tree.docs / "index.html").is_file()


def test_stale_published_html_is_reported(tree: Tree) -> None:
    publish(tree)
    assert check_docs.check(tree.cfg()) == []
    (tree.docs / "index.html").write_text("tampered")
    assert any("out of date" in p for p in check_docs.check(tree.cfg()))


def test_unpublished_tree_is_not_flagged_stale(tree: Tree) -> None:
    assert not any("out of date" in p for p in check_docs.check(tree.cfg()))


def test_demo_is_published_with_pinned_cdn_tags_and_no_google_fonts(tree: Tree) -> None:
    head = (
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"'
        ' rel="stylesheet">\n'
    )
    tree.write("apps/webui/oakenmail-templates/login.html", f"<html><head>{head}</head></html>")
    tree.write("apps/webui/oakenmail-templates/assets/x.css", "a{}")
    publish(tree)
    login = (tree.docs / "demo" / "login.html").read_text()
    assert "googleapis" not in login
    assert 'integrity="sha384-' in login and 'crossorigin="anonymous"' in login
    assert (tree.docs / "demo" / "assets" / "x.css").read_text() == "a{}"
    assert "demo/login.html" in (tree.docs / "index.html").read_text()
