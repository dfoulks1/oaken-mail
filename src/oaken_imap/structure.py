# SPDX-License-Identifier: Apache-2.0
"""Whether a parsed IMAP ``BODYSTRUCTURE`` has an attachment. Pure: it reads the server's own
description of the message (RFC 3501 7.4.2), so nothing of the message is downloaded.

A part is an attachment when it is named (a ``name`` or ``filename`` parameter), is marked
``attachment``, or is a whole forwarded message (``message/rfc822``), the same test the parser
applies to the message itself.
"""

from collections.abc import Iterator
from itertools import takewhile
from typing import Any

_NAMES = {b"name", b"filename"}


def _is_list(x: Any) -> bool:
    return isinstance(x, (list, tuple))


def _runs(node: Any) -> Iterator[Any]:
    """Every list inside ``node``, at any depth: the type parameters, the disposition and its
    parameters."""
    for item in node:
        if _is_list(item):
            yield item
            yield from _runs(item)


def _named(run: Any) -> bool:
    low = [x.lower() if isinstance(x, bytes) else x for x in run]
    return bool(low) and (low[0] == b"attachment" or any(k in _NAMES for k in low[::2]))


def has_attachment(node: Any) -> bool:
    if not _is_list(node) or not node:
        return False
    if _is_list(node[0]):  # multipart: the child parts come first, then the subtype
        return any(has_attachment(c) for c in takewhile(_is_list, node))
    if tuple(x.lower() for x in node[:2] if isinstance(x, bytes)) == (b"message", b"rfc822"):
        return True
    return any(_named(run) for run in _runs(node[2:]))
