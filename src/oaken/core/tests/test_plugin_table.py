# SPDX-License-Identifier: Apache-2.0
"""``[plugins.<kind>.<name>]`` is the rule; the flat ``[plugins.<name>]`` is only a fallback."""

from oaken.core.settings import Settings


def test_the_kind_qualified_table_is_read() -> None:
    s = Settings(plugins={"triager": {"ai": {"model": "q"}}})
    assert s.plugin_table("triager", "ai") == {"model": "q"}


def test_it_wins_over_a_flat_table_of_the_same_name() -> None:
    s = Settings(plugins={"ai": {"model": "flat"}, "triager": {"ai": {"model": "q"}}})
    assert s.plugin_table("triager", "ai") == {"model": "q"}


def test_the_flat_table_is_the_fallback_and_missing_is_empty() -> None:
    s = Settings(plugins={"summary": {"min_priority": 2}})
    assert s.plugin_table("enricher", "summary") == {"min_priority": 2}
    assert s.plugin_table("enricher", "nope") == {}


def test_two_kinds_may_share_a_name() -> None:
    s = Settings(plugins={"embedder": {"ollama": {"a": 1}}, "llm_backend": {"ollama": {"b": 2}}})
    assert s.plugin_table("embedder", "ollama") == {"a": 1}
    assert s.plugin_table("llm_backend", "ollama") == {"b": 2}
