# SPDX-License-Identifier: Apache-2.0
"""The data behind core's home page (``/``), rendered from ``welcome.html``.

A friendly landing page served by core itself: it tells a person the daemon is up, what version it
is, and where its files live (``OAKEN_HOME``, config and data directories, the port), then points
at ``/stats``, ``/config``, ``/getting_started`` and the hosted docs. Like ``/stats`` it is
loopback-only and unauthenticated, and it carries **no mail content** — only a version, a few
paths, a status flag and counts.
"""

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from oaken.core import paths

_TEMPLATES = Path(__file__).resolve().parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES)),
    autoescape=select_autoescape(["html", "xml"]),
)

DOCS_URL = "https://dfoulks1.github.io/oaken-mail/"

# The config editor's one same-origin script, served at /config.js (script-src 'self').
CONFIG_JS = (_TEMPLATES / "config.js").read_text(encoding="utf-8")

# The welcome page's one same-origin script (theme toggle), served at /welcome.js.
WELCOME_JS = (_TEMPLATES / "welcome.js").read_text(encoding="utf-8")

# The jobs & pipeline console's one same-origin script, served at /jobs.js.
JOBS_JS = (_TEMPLATES / "jobs.js").read_text(encoding="utf-8")


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


def render_welcome(context: dict[str, Any]) -> str:
    """Render ``welcome.html`` with the context. Autoescaped. Falls back is the caller's job: if
    the template is missing this raises, and the API serves its plain placeholder instead."""
    return _env.get_template("welcome.html").render(**context)
