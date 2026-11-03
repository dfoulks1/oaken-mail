# SPDX-License-Identifier: Apache-2.0
"""What the model-backed enrichers share: their settings and the base that holds the model handle."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from oaken.plugins.base import BaseEnricher
from oaken.plugins.contracts import CleanMessage
from oaken.plugins.kit import ModelHandle


class EnricherConfig(BaseModel):
    """The ``[plugins.<name>]`` settings every model-backed enricher shares. Each enricher may
    subclass this to add its own. The field descriptions are shown as help text in the ``/general``
    config form, so keep them plain and about what the setting does."""

    model_config = ConfigDict(extra="forbid")

    skip_bulk: bool | None = Field(
        default=None,
        description="Skip bulk/automated mail (newsletters, notifications) for this enricher, to "
        "save model calls. Leave unset to use the enricher's own default.",
    )
    model: str | None = Field(
        default=None,
        description="Which installed model this enricher runs on. Leave as the backend default "
        "unless you want this one enricher on a different model.",
    )


class GatedConfig(EnricherConfig):
    """For enrichers that triage's priority can gate (summary, actions)."""

    min_priority: int | None = Field(
        default=None,
        ge=0,
        le=99,
        description="Skip mail that triage rated below this (0 ignore, 1 low, 2 normal, 3 soon) "
        "to save model calls. Leave unset to run on everything. Needs the triage enricher on; "
        "mail triage has not rated yet is never skipped.",
    )


class LLMEnricher(ModelHandle, BaseEnricher):
    """Base of the model-backed enrichers: ``self.llm`` and ``self.tokenizer`` after ``start``."""

    needs_llm = True
    skip_bulk: ClassVar[bool] = False  # a default, overridden by [plugins.<name>] skip_bulk
    config_model = EnricherConfig

    def needs_model(self, msg: CleanMessage) -> bool:
        """False when this message can be settled without a model (bulk mail, a header rule): core
        then runs the enricher itself and an external agent is never asked for it."""
        return True

    def skips_bulk(self) -> bool:
        chosen = self.config.skip_bulk if self.config is not None else None
        return self.skip_bulk if chosen is None else chosen
