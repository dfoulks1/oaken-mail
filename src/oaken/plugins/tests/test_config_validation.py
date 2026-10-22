# SPDX-License-Identifier: Apache-2.0
"""A plugin validates its own ``[plugins.<name>]`` table against ``config_model`` at start.

Step 34.4 of the core-stabilization plan: a bad config section becomes a clear, field-level
unhealthy message instead of a late failure inside the plugin, core keeps running, and a reload
(which rebuilds the host) picks up the change. A plugin that declares no ``config_model`` is
unaffected — its ``ctx.config`` stays the free-form dict.
"""

from oaken.core.settings import Settings
from oaken.plugins.tests.conftest import Installer
from oaken.plugins.tests.test_host import make_host

# An embedder that declares a pydantic config_model requiring a positive int `shards`.
VALIDATED = """
from pydantic import BaseModel, Field
from oaken.plugins.base import BaseEmbedder
from oaken.plugins.contracts import Health

class Config(BaseModel):
    shards: int = Field(ge=1)

class Validated(BaseEmbedder):
    model_id = "validated"
    dimensions = 2
    version = "1"
    config_model = Config

    async def embed(self, texts):
        return [[0.0, 0.0] for _ in texts]
"""

# An embedder with no config_model: its config is never validated.
FREEFORM = """
from oaken.plugins.base import BaseEmbedder

class Freeform(BaseEmbedder):
    model_id = "freeform"
    dimensions = 2
    version = "1"
    seen = []

    async def start(self, ctx):
        await super().start(ctx)
        Freeform.seen.append(dict(ctx.config))

    async def embed(self, texts):
        return [[0.0, 0.0] for _ in texts]
"""


async def test_a_valid_config_section_starts_the_plugin_healthy(install_plugin: Installer) -> None:
    install_plugin("plug_valid", VALIDATED, [("oaken.embedders", "shardy", "Validated")])
    host, _, _ = make_host(Settings(plugins={"shardy": {"shards": 4}}))
    await host.start()
    try:
        [st] = host.statuses()
        assert (st.name, st.started, st.healthy) == ("shardy", True, True)
        assert host.embedder("shardy").model_id == "validated"
    finally:
        await host.stop()


async def test_an_invalid_config_section_is_unhealthy_with_the_field_named(
    install_plugin: Installer,
) -> None:
    install_plugin("plug_invalid", VALIDATED, [("oaken.embedders", "shardy", "Validated")])
    host, _, seen = make_host(Settings(plugins={"shardy": {"shards": 0}}))  # ge=1 violated
    await host.start()
    try:
        [st] = host.statuses()
        assert st.started is False and st.healthy is False
        assert "shards" in st.detail and "config" in st.detail.lower()
        # core carried on: the host is up and reports the one unhealthy plugin, not a crash.
        assert [(e.plugin, e.healthy) for e in seen] == [("embedder:shardy", False)]
    finally:
        await host.stop()


async def test_a_plugin_without_a_config_model_is_unaffected(install_plugin: Installer) -> None:
    install_plugin("plug_free", FREEFORM, [("oaken.embedders", "freeo", "Freeform")])
    # A section the plugin never declared a schema for is handed through untouched.
    host, _, _ = make_host(Settings(plugins={"freeo": {"anything": "goes", "n": 7}}))
    await host.start()
    try:
        [st] = host.statuses()
        assert (st.started, st.healthy) == (True, True)
    finally:
        await host.stop()
    from plug_free import Freeform  # type: ignore[import-not-found]

    assert Freeform.seen == [{"anything": "goes", "n": 7}]


async def test_a_reload_from_a_bad_to_a_good_section_recovers(install_plugin: Installer) -> None:
    install_plugin("plug_reload", VALIDATED, [("oaken.embedders", "shardy", "Validated")])
    # First host: bad config -> unhealthy (as a running daemon would be after a bad edit).
    bad, _, _ = make_host(Settings(plugins={"shardy": {"shards": 0}}))
    await bad.start()
    try:
        assert bad.statuses()[0].healthy is False
    finally:
        await bad.stop()
    # Reload rebuilds the host on fresh settings (lifecycle._reload builds a new Core); a corrected
    # section now starts healthy with no other machinery.
    good, _, _ = make_host(Settings(plugins={"shardy": {"shards": 2}}))
    await good.start()
    try:
        assert good.statuses()[0].healthy is True
    finally:
        await good.stop()
