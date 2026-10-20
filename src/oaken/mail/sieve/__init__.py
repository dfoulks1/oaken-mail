# SPDX-License-Identifier: Apache-2.0
"""Sieve (RFC 5228) filtering. Pure: no database, no network, no model. A script decides a
disposition and labels, flags and suppressed intents; it never delivers, sends or deletes."""

from oaken.mail.sieve.interp import (
    Disposition,
    Effect,
    Envelope,
    MessageView,
    SieveResult,
    run,
)
from oaken.mail.sieve.parser import SieveError
from oaken.mail.sieve.script import REFUSED, SUPPORTED, Script, compile_script
from oaken.mail.sieve.view import derive_envelope, view_of

__all__ = [
    "REFUSED",
    "SUPPORTED",
    "Disposition",
    "Effect",
    "Envelope",
    "MessageView",
    "Script",
    "SieveError",
    "SieveResult",
    "compile_script",
    "derive_envelope",
    "run",
    "view_of",
]
