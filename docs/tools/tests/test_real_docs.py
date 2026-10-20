# SPDX-License-Identifier: Apache-2.0
"""The real docs/ tree must stay buildable and clean."""

from pathlib import Path

import build_site
import check_docs
from model import load_config

ROOT = Path(__file__).resolve().parents[3]


def test_real_docs_pass_check() -> None:
    cfg = load_config(ROOT / "docs", ROOT)
    assert check_docs.check(cfg) == []


def test_real_site_has_home_and_a_content_page(tmp_path: Path) -> None:
    cfg = load_config(ROOT / "docs", ROOT)
    files = build_site.build(cfg, tmp_path / "site")
    assert "index.html" in files
    assert "architecture/overview.html" in files
    assert "adr/index.html" in files
    home = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "Oaken Mail" in home
    assert "Architecture overview" in home
