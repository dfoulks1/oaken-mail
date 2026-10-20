# SPDX-License-Identifier: Apache-2.0
"""What a handler receives besides its input."""

from dataclasses import dataclass

from oaken.core.operations.caller import CallerContext
from oaken.core.operations.exposure import ExposureStore
from oaken.core.operations.limits import Limits
from oaken.core.operations.registry import OperationRegistry
from oaken.core.operations.services import Services


@dataclass(frozen=True)
class OpContext:
    """``caller`` says who and through which door. By the time a mutating handler runs, the gate
    has approved the call: a handler never decides that for itself, and never trusts a flag that
    came in with the input."""

    svc: Services
    caller: CallerContext
    limits: Limits
    registry: OperationRegistry
    exposure: ExposureStore
