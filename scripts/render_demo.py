#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Render the Core console demo from the real templates.

Starts the console in-process over the test rig (the same registry and stores the API tests use),
fetches each page, and writes it as a static file under ``apps/core/oakencore-templates/`` with
the real ``oaken.css``. ``docs/tools/build_site.py`` publishes that folder to ``docs/demo-core/``.
Run it after the console templates change:

    uv run python scripts/render_demo.py
"""

import asyncio
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "apps" / "core" / "oakencore-templates"
STATIC = ROOT / "src" / "oaken" / "api" / "static"
TEMPLATES = ROOT / "src" / "oaken" / "api" / "templates"
# console path -> demo file
PAGES = {
    "/": "index.html",
    "/general": "general.html",
    "/pipeline?tab=provider": "pipeline.html",
    "/pipeline?tab=sieve": "pipeline-sieve.html",
    "/pipeline?tab=llm_backend": "pipeline-llm_backend.html",
    "/pipeline?tab=vector_store": "pipeline-vector_store.html",
    "/pipeline?tab=cache_store": "pipeline-cache_store.html",
    "/triage": "triage.html",
    "/embed": "embed.html",
    "/enrich": "enrich.html",
    "/action": "action.html",
    "/config": "config.html",
    "/capabilities": "capabilities.html",
    "/getting-started": "getting_started.html",
}
RIBBON = (
    '<div style="background:#8b5a2b;color:#fff;text-align:center;padding:.35rem 1rem;'
    'font:600 .8rem/1.3 system-ui,sans-serif">Demo snapshot of Oaken Core\'s console. Sample data; '
    'the controls do nothing. <a style="color:#fff" href="index.html">Back to the Core demo</a></div>'
)


def local(path: str) -> str:
    """The demo file a console link points at."""
    if path in PAGES:
        return PAGES[path]
    bare = path.split("?")[0]
    return PAGES.get(bare, PAGES.get(bare.rstrip("/"), "index.html"))


def rewrite(html: str) -> str:
    html = re.sub(r'(href|src)="/oaken\.css\?v=[^"]*"', r'\1="assets/oaken.css"', html)
    html = html.replace('"/oaken-logo.png"', '"assets/oaken-logo.png"')
    html = re.sub(r'<script src="/[^"]*"></script>', "", html)  # a snapshot does not refresh

    def link(m: "re.Match[str]") -> str:
        target = m.group(1).replace("&amp;", "&")
        if target.startswith("/ui/") or target.startswith("/v1/"):
            return 'href="#"'
        return f'href="{local(target)}"'

    html = re.sub(r'href="(/[^"]*)"', link, html)
    html = re.sub(r'(<form\b[^>]*?)action="[^"]*"', r'\1action="#"', html)
    html = re.sub(r"<(button|input|select|textarea)\b", r"<\1 disabled", html)
    html = re.sub(r'(name="csrf" value=")[0-9a-f]+"', r'\1demo"', html)
    return re.sub(r"(<body[^>]*>)", r"\1" + RIBBON.replace("\\", "\\\\"), html, count=1)


async def main() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from oaken.api.tests.conftest import build_rig  # noqa: PLC0415

    OUT.mkdir(parents=True, exist_ok=True)
    assets = OUT / "assets"
    assets.mkdir(exist_ok=True)
    shutil.copy(STATIC / "oaken.css", assets / "oaken.css")
    shutil.copy(TEMPLATES / "oaken-logo.png", assets / "oaken-logo.png")
    with tempfile.TemporaryDirectory() as tmp:
        async with build_rig(Path(tmp)) as rig:
            for path, name in PAGES.items():
                resp = await rig.get(path)
                if resp.status_code != 200:
                    print(f"skip {path}: {resp.status_code}", file=sys.stderr)
                    continue
                (OUT / name).write_text(rewrite(resp.text), encoding="utf-8")
                print(f"wrote {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
