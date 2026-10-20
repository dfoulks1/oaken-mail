# SPDX-License-Identifier: Apache-2.0
"""The Oaken command-line client.

The CLI is a *thin* client: it holds no business logic. Every command is a call to a registry
operation, reached through an :class:`~oaken_cli.client.ApiClient`. In normal use that client
speaks HTTP to the local core daemon (prompt 16); in tests it wraps the in-process
``CoreClient`` directly, so the whole CLI is exercised without a socket.

The entry point is :func:`oaken_cli.main.main`, wired as the ``oaken`` console script.
"""

from oaken_cli.main import main

__all__ = ["main"]
