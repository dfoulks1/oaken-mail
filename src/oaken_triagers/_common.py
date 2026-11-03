# SPDX-License-Identifier: Apache-2.0
"""What the model-backed triagers share: their settings and the base that holds the model handle."""

from pydantic import BaseModel, ConfigDict, Field

from oaken.plugins.base import BaseTriager
from oaken.plugins.kit import ModelHandle


class TriagerConfig(BaseModel):
    """The ``[plugins.triager.<name>]`` settings a model-backed triager has. The field description
    is shown as help text in the settings form, so keep it plain."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(
        default=None,
        description="Which installed model this triager runs on. Leave as the backend default "
        "unless you want this one triager on a different model.",
    )

    guard: str | None = Field(
        default=None,
        description="Override the guard: the fixed text that tells the model mail is data, never "
        "instructions. Leave blank for the built-in. Weakening it lowers injection defenses.",
    )
    prompt: str | None = Field(
        default=None,
        description="Override the task prompt this plugin sends the model. Leave blank for the "
        "built-in. Changing it changes what is cached.",
    )


class LLMTriager(ModelHandle, BaseTriager):
    """A triager that asks a model: ``self.llm`` and ``self.tokenizer`` after ``start``. It runs
    after the deterministic ones (higher ``rank``)."""

    needs_llm = True
    rank = 90
    config_model = TriagerConfig
