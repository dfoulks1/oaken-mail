# SPDX-License-Identifier: Apache-2.0
"""The one-message summary a person asks for (the ``summarize_message`` operation).

This is not the summary enricher (prompt 14), which runs at ingest on every message. This runs when
someone asks, and goes through the same cached, workspace-checked path, so asking twice costs
nothing and a ``secure`` workspace only ever reaches a local model.
"""

from pydantic import Field

from oaken.ai.prompts import PromptBuilder, SafeOutput

# Characters of one message's cleaned text sent to the model; the rest is cut, not summarized.
MAX_BODY_CHARS = 12_000


class SummaryOutput(SafeOutput):
    summary: str = Field(min_length=1, max_length=2000)


SUMMARY = PromptBuilder(
    "summarize",
    "1",
    "Summarize the email in at most four plain sentences: who wants what, any date or amount, "
    "and what is being asked of the reader. Do not quote the message at length and do not add "
    "anything that is not in it. Put the summary in the summary field.",
    SummaryOutput,
    max_output_tokens=400,
)
