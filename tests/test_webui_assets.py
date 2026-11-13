# SPDX-License-Identifier: Apache-2.0
"""The browser UI loads nothing from another origin: every stylesheet, script and font is served from this
server (assets/vendor/), and the CSP allows no other host. The page can read all mail through the proxy, so
third-party code in it would see everything."""

import re
from pathlib import Path

from oaken_webui.app import build_app

ROOT = Path(__file__).parents[1]
UI = ROOT / "oakenmail-templates"
HOST = {"host": "127.0.0.1:62584"}

# Every src= or href= that loads something into the page (links to other pages are <a href>, not checked here).
_LOADS = re.compile(
    r'<(?:script|link|img|source|iframe)\b[^>]*\b(?:src|href)="([^"]+)"', re.IGNORECASE
)
_CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+)")


def _local(ref: str) -> bool:
    return not re.match(
        r"^(?:[a-z][a-z0-9+.-]*:)?//", ref, re.IGNORECASE
    ) and not ref.lower().startswith(("http:", "https:"))


def test_pages_load_only_local_assets():
    for page in sorted(UI.glob("*.html")):
        for ref in _LOADS.findall(page.read_text()):
            assert _local(ref), f"{page.name} loads {ref} from another origin"
            path = ref.split("?", 1)[0]
            assert (UI / path).is_file(), (
                f"{page.name} loads {ref}, which is not in the UI"
            )


def test_vendored_css_references_only_local_files():
    for css in sorted((UI / "assets").rglob("*.css")):
        for ref in _CSS_URL.findall(css.read_text()):
            if ref.startswith("data:"):
                continue
            assert _local(ref), f"{css.relative_to(UI)} loads {ref} from another origin"
            assert (css.parent / ref.split("?", 1)[0]).is_file(), (
                f"{css.relative_to(UI)}: {ref} is missing"
            )


async def test_csp_allows_no_other_origin():
    app = build_app(
        static_dir=UI, client_factory=lambda: None
    )  # serving a page never calls core
    r = await app.test_client().get("/", headers=HOST)
    csp = r.headers["content-security-policy"]
    assert r.status_code == 200
    assert "http:" not in csp and "https:" not in csp and "*" not in csp
    assert "script-src 'self';" in csp


async def test_vendored_assets_are_served():
    c = build_app(static_dir=UI, client_factory=lambda: None).test_client()
    for path in (
        "assets/vendor/bootstrap/bootstrap.bundle.min.js",
        "assets/vendor/bootstrap/bootstrap.min.css",
        "assets/vendor/bootstrap-icons/bootstrap-icons.min.css",
        "assets/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2",
        "assets/vendor/fonts/fonts.css",
        "assets/vendor/fonts/figtree-latin-wght-normal.woff2",
    ):
        assert (await c.get("/" + path, headers=HOST)).status_code == 200, path
