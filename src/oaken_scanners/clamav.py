# SPDX-License-Identifier: Apache-2.0
"""``clamav``: scan quarantined attachments with ``clamd`` (CORE_57, ADR 0020).

A ``BaseScanner``, so it writes one method: ``scan(path) -> Verdict``. The base runs it on the
``scan`` route for every attachment still owed a verdict, opens the file 0400 only inside the block,
and records the result. This plugin streams the file to ``clamd`` over ``INSTREAM`` (so ``clamd``
needs no permission on a file it may not own) and turns the answer into a verdict:

- ``stream: OK`` -> ``clean``
- ``stream: <signature> FOUND`` -> ``infected`` (the signature is the detail)
- clamd down, a timeout, or an ``ERROR`` -> ``error`` (never ``clean``; the file stays held and
  ``oaken reprocess scan`` retries it)

``clamd`` is a separate process this plugin only talks to; no ClamAV code is copied in.
"""

import asyncio
from pathlib import Path

from oaken.core.errors import PluginError
from oaken.core.logging import get_logger
from oaken.plugins.base import BaseScanner
from oaken.plugins.contracts import Health, Verdict
from oaken_scanners.clamd import ClamdError, file_blocks, instream
from oaken_scanners.config import ClamavConfig

log = get_logger("oaken.clamav")

SETUP_HELP = (
    "clamav scans quarantined attachments with ClamAV's clamd daemon, and works only while clamd "
    "is running: this plugin talks to clamd, it does not run ClamAV itself. Install ClamAV and "
    "start clamd, then point this plugin at its unix socket (usual) or its host and port. While "
    "clamd is down the plugin is unhealthy and every scan is an `error` (files stay held, never "
    "released), so nothing unsafe gets out; start clamd and `oaken reprocess scan` retries them. "
    "When you first enable it, rescan the files already held with `oaken reprocess scan`."
)
CONFIG_EXAMPLE = '[plugins.action.clamav]\nenabled = true\nsocket = "/var/run/clamav/clamd.ctl"\n'


class ClamavScanner(BaseScanner):
    version = "1"
    config_model = ClamavConfig
    enabled_default = False  # reaches another process; opt in
    setup_help = SETUP_HELP
    config_example = CONFIG_EXAMPLE

    def _cfg(self) -> ClamavConfig:
        if not isinstance(self.config, ClamavConfig):
            raise PluginError("clamav is not configured")
        return self.config

    async def scan(self, path: Path) -> Verdict:
        cfg = self._cfg()
        try:
            result = await instream(
                file_blocks(str(path)),
                socket=cfg.socket,
                host=cfg.host,
                port=cfg.port,
                timeout=cfg.timeout_seconds,
            )
        except ClamdError as exc:
            log.warning("clamav scan could not finish", reason=str(exc))
            return Verdict.error(str(exc))
        if result.infected:
            return Verdict.infected(result.signature)
        return Verdict.clean()

    async def health(self) -> Health:
        """clamd answers its ``PING`` with ``PONG``. Unhealthy (not failed) when it does not, so the
        plugin stays loaded and scans become ``error`` until clamd is back."""
        try:
            await _ping(self._cfg())
        except ClamdError as exc:
            return Health(False, f"clamd not reachable: {exc}")
        return Health(True)


async def _ping(cfg: ClamavConfig) -> None:
    """Open the connection, send ``PING``, expect ``PONG``, over clamd's configured transport.
    Raises ``ClamdError`` when clamd is not there or does not answer."""
    if cfg.socket:
        opener = asyncio.open_unix_connection(cfg.socket)
    else:
        opener = asyncio.open_connection(cfg.host, cfg.port)
    try:
        reader, writer = await asyncio.wait_for(opener, cfg.timeout_seconds)
    except (TimeoutError, OSError) as exc:
        raise ClamdError(str(exc)) from exc
    try:
        writer.write(b"zPING\x00")
        await writer.drain()
        reply = await asyncio.wait_for(reader.readline(), cfg.timeout_seconds)
    except (TimeoutError, OSError) as exc:
        raise ClamdError(str(exc)) from exc
    finally:
        writer.close()
    if b"PONG" not in reply:
        raise ClamdError("clamd did not answer PING")
