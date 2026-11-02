# SPDX-License-Identifier: Apache-2.0
"""``oaken-enrichers``: the first-party enrichers, registered in the ``oaken.enrichers`` group.

``summary``, ``actions`` and the ``ai`` triager ask a model (through ``ctx.llm``, never
directly);
``inspector`` reads headers and MIME structure and never does. Core does not import this package.
Each can be switched off with ``[plugins.<name>] enabled = false``.
"""

from oaken_enrichers.actions import ActionsEnricher
from oaken_enrichers.inspector import InspectorEnricher
from oaken_enrichers.summary import SummaryEnricher
from oaken_enrichers.triage import AITriager, RulesTriager

__all__ = ["AITriager", "ActionsEnricher", "InspectorEnricher", "RulesTriager", "SummaryEnricher"]
