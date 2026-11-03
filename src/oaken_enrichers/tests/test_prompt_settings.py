# SPDX-License-Identifier: Apache-2.0
"""``guard`` and ``prompt`` are settings of every model-backed plugin, and ``builder()`` applies
them: blank means the built-in, anything else replaces that part alone."""

import pytest
from pydantic import ValidationError

from oaken.ai.prompts import DATA_RULES, PromptBuilder, SafeOutput
from oaken.plugins.kit import ModelHandle
from oaken_enrichers._common import EnricherConfig, GatedConfig
from oaken_triagers._common import TriagerConfig

B = PromptBuilder("t", "1", "Do the task.", SafeOutput)


class Handle(ModelHandle):
    prompt = B


def handle(config: object) -> Handle:
    h = Handle()
    h.config = config
    return h


@pytest.mark.parametrize("model", [EnricherConfig, GatedConfig, TriagerConfig])
def test_every_model_config_accepts_guard_and_prompt(model: type) -> None:
    cfg = model.model_validate({"guard": "g", "prompt": "p"})
    assert (cfg.guard, cfg.prompt) == ("g", "p")
    with pytest.raises(ValidationError):
        model.model_validate({"promt": "typo"})  # extra keys are still refused


def test_no_override_uses_the_built_in_builder() -> None:
    assert handle(EnricherConfig()).builder() is B
    assert handle(EnricherConfig(prompt="", guard="")).builder() is B  # a saved blank is none
    assert handle(None).builder() is B


def test_the_prompt_setting_replaces_the_task_and_keeps_the_guard() -> None:
    built = handle(TriagerConfig(prompt="Rate it.")).builder()
    assert built.system == f"{DATA_RULES}\n\nRate it."


def test_the_guard_setting_replaces_the_guard_and_keeps_the_task() -> None:
    built = handle(EnricherConfig(guard="Be careful.")).builder()
    assert built.system == "Be careful.\n\nDo the task."
