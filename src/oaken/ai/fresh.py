# SPDX-License-Identifier: Apache-2.0
"""A per-task switch for "ask the model again". Kept in a module of its own so the ingest pipeline
and the language-model service can both use it without importing each other."""

import contextvars

# Set while a person asks for a fresh answer (``oaken enrich run --force``): the answer cache is not
# read, but the new answer is still stored. A context variable, so it follows the task only.
FRESH: contextvars.ContextVar[bool] = contextvars.ContextVar("llm_fresh", default=False)
