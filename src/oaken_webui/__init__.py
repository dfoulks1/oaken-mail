# SPDX-License-Identifier: LicenseRef-Proprietary
"""Oaken WebUI: the browser UI server (Quart). Talks to ``oaken_webui_core``, which talks to
oaken-mail's general API. The entry point is :func:`oaken_webui.main.main`."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("oaken-mail")  # one version for the whole product
except PackageNotFoundError:  # a bare checkout, not installed
    __version__ = "0.0.0"

__all__ = ["__version__"]
