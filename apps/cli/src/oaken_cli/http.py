# SPDX-License-Identifier: Apache-2.0
"""The HTTP :class:`~oaken_cli.client.ApiClient` — a **stub for prompt 16**.

The real transport is built in prompt 16 (the local API daemon). This file exists now so the CLI
has one place to reach for when it talks to a running daemon, and so the wire contract the server
must serve is written down from the client's side. Everything here raises
:class:`~oaken_cli.exits.CoreUnavailable` or ``NotImplementedError`` with a message that names
prompt 16; nothing performs real I/O yet.

Wire contract (what prompt 16 must serve, from the generated OpenAPI in
``oaken.core.operations.openapi``)
---------------------------------------------------------------------------
* Base URL and token come from :func:`oaken_cli.discovery.discover` (the runtime file / token
  file, or ``--url``/``--token-file``). Every request carries ``Authorization: Bearer <token>``.
* ``GET  /v1/health`` — unauthenticated liveness; ``{"status": "...", "version": "..."}``.
* ``GET  /v1/operations`` — the catalogue: for each operation ``name``, ``summary``,
  ``permission``, ``marks_read``, ``streaming``, and its ``input``/``output`` JSON Schema. Decodes
  into :class:`~oaken_cli.client.OpInfo`.
* ``POST /v1/operations/{name}`` — body ``{"input": {...}, "confirm": bool,
  "confirmation_token": str|null}``; ``Content-Type: application/json``; the CLI sends
  ``X-Oaken-Client: cli``. Response is ``{"request_id", "status": "ok"|"confirmation_required",
  "output": {...}|null, "proposal": {...}|null}``, decoded into
  :class:`~oaken_cli.client.CallResult`.
* ``GET  /v1/events`` — Server-Sent Events; resume with ``Last-Event-ID``. Each event decodes into
  :class:`~oaken_cli.client.Event` (``id``, ``event``, ``data``).

Error mapping (server → CLI): an HTTP error body ``{"code", "message"}`` maps through
:func:`oaken_cli.exits.exit_for_error_code`; ``401`` → exit 4 (bad token); a refused connection or
a ``503`` during start → :class:`CoreUnavailable` (exit 3).

Implementation notes for prompt 16's author
-------------------------------------------
Use ``httpx`` (``httpx2`` is already a dependency). Hold an ``httpx.AsyncClient`` and close it in
:meth:`aclose`. For SSE, read the response line by line and parse ``id:``/``event:``/``data:``
fields; keep the last id so ``oaken watch`` can reconnect. Keep this class the *only* place that
knows the wire format — the CLI commands must stay transport-neutral.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from oaken_cli.client import ApiClient, CallResult, Event, OpInfo
from oaken_cli.discovery import Endpoint

_PROMPT16 = "the HTTP transport is not built yet (prompt 16: local API daemon)"


class HttpApiClient(ApiClient):
    """Talks to the running daemon over loopback HTTP. **Not implemented until prompt 16.**"""

    def __init__(self, endpoint: Endpoint) -> None:
        self.endpoint = endpoint

    async def health(self) -> Mapping[str, Any]:
        raise NotImplementedError(_PROMPT16)

    async def list_ops(self) -> list[OpInfo]:
        raise NotImplementedError(_PROMPT16)

    async def call(
        self,
        name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        confirm: bool = False,
        confirmation_token: str | None = None,
    ) -> CallResult:
        raise NotImplementedError(_PROMPT16)

    async def stream_events(self, last_event_id: str | None = None) -> AsyncIterator[Event]:
        raise NotImplementedError(_PROMPT16)
        yield  # pragma: no cover  — makes this an async generator for typing
