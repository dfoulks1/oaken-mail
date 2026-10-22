# SPDX-License-Identifier: Apache-2.0
"""``oaken usage``: the usage table with a totals row under a rule, and the savings tables."""

from __future__ import annotations

import io
import json

from oaken_cli.exits import ExitCode
from oaken_cli.output import print_table
from oaken_cli.tests.conftest import Cli


def test_footer_rows_sit_under_a_rule() -> None:
    out = io.StringIO()
    print_table(
        [{"a": "x", "n": 1}, {"a": "y", "n": 2}, {"a": "total", "n": 3}],
        ["a", "n"],
        stream=out,
        footer_rows=1,
    )
    lines = out.getvalue().splitlines()
    rules = [i for i, ln in enumerate(lines) if set(ln) <= {"-", " "} and ln.strip()]
    assert len(rules) == 2 and lines[rules[1] + 1].startswith("total")
    assert lines[rules[1] - 1].startswith("y")  # the rule falls between the data and the total


async def test_usage_has_a_totals_row_and_the_savings_by_caller(cli: Cli) -> None:
    usage = cli.ops.svc.need_usage()
    await usage.record("embed", "m", tokens_in=100, caller="cli")
    await usage.record("llm.summary", "m", tokens_in=40, tokens_out=10, cached=True, caller="mcp")
    await usage.record("llm.summary", "m", tokens_in=40, tokens_out=10, caller="mcp")
    await usage.record_context(
        "list_messages", "mcp", messages=3, tokens_returned=200, tokens_full=900
    )
    await usage.record_context("get_thread", "ui", messages=2, tokens_returned=100, tokens_full=300)

    run = await cli("usage")
    assert run.code == int(ExitCode.OK)
    assert "\ntotal" in run.out  # the usage table's totals row
    assert "saved, by caller" in run.out and "mcp" in run.out and "ui" in run.out
    assert "context kept out of the client" in run.out

    js = json.loads((await cli("usage", "--json")).out)
    assert js["rows"] and "total" not in {r["workspace"] for r in js["rows"]}  # JSON stays plain
    by_caller = {t["caller"]: t for t in js["savings"]["totals"]}
    assert by_caller["mcp"]["context_tokens_saved"] == 700
    assert by_caller["mcp"]["cache_tokens_saved"] == 50
