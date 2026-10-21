# SPDX-License-Identifier: Apache-2.0
"""The core operations, and the function that puts them in a registry."""

from oaken.core.operations.builtin import (
    accounts,
    agents,
    enrichment,
    filtering,
    jobs,
    mail,
    messages,
    schedules,
    system,
)
from oaken.core.operations.registry import AnyOperation, OperationRegistry


def core_operations() -> tuple[AnyOperation, ...]:
    return (
        *messages.OPERATIONS,
        *mail.OPERATIONS,
        *schedules.OPERATIONS,
        *system.OPERATIONS,
        *accounts.OPERATIONS,
        *agents.OPERATIONS,
        *enrichment.OPERATIONS,
        *filtering.OPERATIONS,
        *jobs.OPERATIONS,
    )


def agent_registry() -> OperationRegistry:
    """The few operations an external agent may call on its own door (the swarm)."""
    return jobs.agent_registry()


def core_registry() -> OperationRegistry:
    """A registry holding every core operation. Plugin operations are attached by the daemon."""
    return OperationRegistry(core_operations())


__all__ = ["agent_registry", "core_operations", "core_registry"]
