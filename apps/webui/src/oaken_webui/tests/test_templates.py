# SPDX-License-Identifier: Apache-2.0
"""Every page that mounts the shared sidebar must load ``api.js`` before ``oaken.js``.

``oaken.js`` builds the sidebar and wires the page only when ``window.Oaken.api`` exists, and
otherwise does nothing without an error, so a page that forgets ``api.js`` shows an empty sidebar.
"""

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[3] / "oakenmail-templates"
PAGES = sorted(p for p in TEMPLATES.glob("*.html") if "data-oaken-sidebar" in p.read_text())


def test_there_are_pages_with_the_shared_sidebar() -> None:
    assert len(PAGES) >= 6


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_api_js_is_loaded_before_oaken_js(page: Path) -> None:
    scripts = re.findall(r'<script[^>]*\bsrc="([^"]+)"', page.read_text())
    local = [s.split("?")[0] for s in scripts if s.startswith("assets/")]
    assert "assets/api.js" in local, f"{page.name} never loads assets/api.js"
    assert local.index("assets/api.js") < local.index("assets/oaken.js")


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_pages_carry_the_demo_marker_the_live_server_strips(page: Path) -> None:
    """The published demo has no server: a page without ``data-oaken-demo`` would call an API
    that is not there. ``oaken-webui`` strips the marker, so the live UI is unaffected."""
    html_tag = re.search(r"<html[^>]*>", page.read_text())
    assert html_tag is not None and "data-oaken-demo" in html_tag.group(0)
