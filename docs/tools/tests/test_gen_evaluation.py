# SPDX-License-Identifier: Apache-2.0
"""The Evaluation results page is the stored JSON, formatted: it adds and rounds nothing else."""

import json
import shutil
from pathlib import Path

import build_site
import pytest
from model import BuildError, load_config

ROOT = Path(__file__).resolve().parents[3]


def body() -> str:
    cfg = load_config(ROOT / "docs", ROOT)
    return next(p for p in build_site.collect_pages(cfg) if p.id == "reference/evaluation").body_md


def test_the_page_shows_each_retriever_with_the_date_and_seed_of_the_run() -> None:
    data = json.loads((ROOT / "docs" / "evals" / "latest.json").read_text())
    text = body()
    assert data["date"] in text and f"seed **{data['seed']}**" in text
    for label in ("Keyword only", "Vector only", "Hybrid (RRF", "Hybrid, plain RRF"):
        assert label in text
    hybrid = data["retrieval"]["hybrid"]["all"]["ndcg_10"]
    assert f"{hybrid:.3f}" in text
    assert "Evidence tokens saved vs the baseline" in text


def test_a_missing_results_file_is_a_build_error_not_an_empty_page(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    shutil.copytree(ROOT / "docs", docs, ignore=shutil.ignore_patterns("evals", "demo", "prompts"))
    cfg = load_config(docs, ROOT)
    with pytest.raises(BuildError, match="evaluation results"):
        build_site.collect_pages(cfg)
