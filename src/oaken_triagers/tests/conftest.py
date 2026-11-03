# SPDX-License-Identifier: Apache-2.0
"""The triager tests use the same rig as the enricher tests: a real pipeline over a temp database
with a fake model. Shared, not copied, so the two cannot drift."""

from oaken_enrichers.tests.conftest import Rig, message, rig, with_llm  # noqa: F401
