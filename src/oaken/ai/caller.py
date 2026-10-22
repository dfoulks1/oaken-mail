# SPDX-License-Identifier: Apache-2.0
"""Which door the running operation came in through, for token accounting. A context variable, so
it follows the task only; background work never sets it and reads as ``internal``. Kept in a module
of its own so the operation client and the usage store can share it without importing each other."""

import contextvars

CALLER: contextvars.ContextVar[str] = contextvars.ContextVar("usage_caller", default="internal")
