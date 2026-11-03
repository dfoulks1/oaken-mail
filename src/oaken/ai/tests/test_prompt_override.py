# SPDX-License-Identifier: Apache-2.0
"""``PromptBuilder.overridden``: the user's guard and task text replace the built-ins separately,
blank keeps the default, and the cache version follows the wording."""

from oaken.ai.prompts import DATA_RULES, PromptBuilder, SafeOutput

B = PromptBuilder("t", "1", "Do the task.", SafeOutput)


def test_blank_overrides_return_the_builder_itself() -> None:
    assert B.overridden(None, None) is B
    assert B.overridden("  ", "") is B


def test_task_override_keeps_the_guard() -> None:
    o = B.overridden(None, "Do it my way.")
    assert o.system == f"{DATA_RULES}\n\nDo it my way."
    assert o.guard == DATA_RULES and o.instructions == "Do it my way."


def test_guard_override_keeps_the_task() -> None:
    o = B.overridden("My guard.", None)
    assert o.system == "My guard.\n\nDo the task."


def test_the_original_is_untouched_and_the_version_changes() -> None:
    o = B.overridden("g", "i")
    assert B.system == f"{DATA_RULES}\n\nDo the task."
    assert o.prompt_version != B.prompt_version
