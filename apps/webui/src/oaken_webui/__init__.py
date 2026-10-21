# SPDX-License-Identifier: Apache-2.0
"""The Oaken Mail web UI server.

A *thin* client, in the same spirit as ``oaken_cli``: it serves the static browser UI and
reverse-proxies the general API (``/v1/*``) to the running core daemon. It contains no business
logic — every mail action is a registry operation answered by core, reached over the same
HTTP bridge the CLI uses. The MCP surface is never involved.

The entry point is :func:`oaken_webui.main.main`, wired as the ``oaken-webui`` console script.
"""

from oaken_webui.main import main

__all__ = ["main"]
