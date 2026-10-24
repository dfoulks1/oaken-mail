# SPDX-License-Identifier: Apache-2.0
"""The docs' own commands are run, and a broken one is reported with its file and line."""

from pathlib import Path

import check_commands
from model import Config, load_config

ROOT = Path(__file__).resolve().parents[3]


def test_only_marked_bash_blocks_are_picked_up() -> None:
    text = (
        "intro\n\n<!-- check -->\n```bash\n# a comment\noaken --version\n\noaken help sync\n```\n\n"
        "```bash\nrm -rf /\n```\n\n<!-- manual -->\n```bash\noaken start\n```\n"
    )
    assert check_commands.blocks(text) == [(4, ["oaken --version", "oaken help sync"])]


def test_oaken_means_this_checkouts_cli_and_uv_run_is_ignored() -> None:
    plain = check_commands.argv_for("oaken sync --wait   # a trailing comment")
    assert plain[1:3] == ["-m", "oaken_cli"] and plain[3:] == ["sync", "--wait"]
    assert check_commands.argv_for("uv run oaken status")[1:3] == ["-m", "oaken_cli"]


def _cfg_for(tmp_path: Path) -> Config:
    docs = tmp_path / "docs"
    docs.mkdir()
    real = load_config(ROOT / "docs", ROOT)
    return Config(**{**real.__dict__, "docs_dir": docs})


def test_a_failing_command_is_reported_with_file_and_line(tmp_path: Path) -> None:
    cfg = _cfg_for(tmp_path)
    (cfg.docs_dir / "page.md").write_text(
        "<!-- check -->\n```bash\noaken --version\noaken no-such-command\n```\n", encoding="utf-8"
    )
    problems = check_commands.check(cfg)
    assert len(problems) == 1
    assert "docs/page.md:2" in problems[0] and "no-such-command" in problems[0]


def test_a_good_block_is_clean(tmp_path: Path) -> None:
    cfg = _cfg_for(tmp_path)
    (cfg.docs_dir / "ok.md").write_text(
        "<!-- check -->\n```bash\noaken --version\noaken help sync\n```\n", encoding="utf-8"
    )
    assert check_commands.check(cfg) == []
