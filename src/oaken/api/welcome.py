# SPDX-License-Identifier: Apache-2.0
"""The data behind core's home page (``/``), rendered by the console from ``welcome.html``.

A friendly landing page served by core itself: it tells a person the daemon is up, what version it
is, and where its files live (``OAKEN_HOME``, config and data directories, the port), then points
at ``/stats``, ``/config``, ``/getting_started`` and the hosted docs. Like ``/stats`` it is
loopback-only and unauthenticated, and it carries **no mail content** — only a version, a few
paths, a status flag and counts.
"""

from typing import Any

from oaken.core import paths

DOCS_URL = "https://dfoulks1.github.io/oaken-mail/"


def welcome_context(
    *,
    version: str,
    port: int | None,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the template context: version, the daemon's environment (paths and port), and an
    optional short status summary. Paths come from ``oaken.core.paths`` so they match exactly what
    core actually uses, OAKEN_HOME-relocated or not."""
    import os

    home = os.environ.get(paths.HOME_ENV)
    return {
        "version": version,
        "docs_url": DOCS_URL,
        "active": "home",
        "env": {
            "oaken_home": home,  # None when unset (platform defaults are in use)
            "config_dir": str(paths.config_dir()),
            "data_dir": str(paths.data_dir()),
            "port": port,
        },
        "status": status or {},
    }
