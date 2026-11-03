# SPDX-License-Identifier: Apache-2.0
"""``PluginHost.trial``: a throwaway instance on a candidate config, the running one untouched."""

import pytest

from oaken.core.errors import NotFound, PluginError
from oaken.core.settings import Settings
from oaken.plugins.tests.conftest import Installer
from oaken.plugins.tests.test_host import make_host

TRIAGER = """
from pydantic import BaseModel, ConfigDict
from oaken.plugins.base import BaseTriager
from oaken.plugins.contracts import TriageVerdict


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    word: str = "none"


class Words(BaseTriager):
    version = "1"
    config_model = Config
    started = 0
    stopped = 0

    async def start(self, ctx):
        await super().start(ctx)
        Words.started += 1

    async def stop(self):
        Words.stopped += 1

    async def triage(self, msg):
        return TriageVerdict({"word": self.config.word})
"""
ENTRIES = [("oaken.triagers", "words", "Words")]


async def test_a_trial_runs_on_its_own_config_and_stops(install_plugin: Installer) -> None:
    install_plugin("plug_trial", TRIAGER, ENTRIES)
    host = make_host(Settings(plugins={"triager": {"words": {"word": "live"}}}))[0]
    await host.start()
    try:
        async with host.trial("triager", "words", {"word": "candidate"}) as plugin:
            assert (await plugin.triage(None)).data == {"word": "candidate"}
        live = host.get("triager", "words")
        assert (await live.triage(None)).data == {"word": "live"}
    finally:
        await host.stop()


async def test_a_candidate_the_plugin_refuses_or_no_plugin_is_an_error(
    install_plugin: Installer,
) -> None:
    install_plugin("plug_trial2", TRIAGER, ENTRIES)
    host = make_host(Settings())[0]
    await host.start()
    try:
        with pytest.raises(PluginError, match="nope"):
            async with host.trial("triager", "words", {"nope": 1}):
                pass
        with pytest.raises(NotFound):
            async with host.trial("triager", "absent", {}):
                pass
    finally:
        await host.stop()
