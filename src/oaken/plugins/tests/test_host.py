# SPDX-License-Identifier: Apache-2.0
import asyncio
from importlib.metadata import EntryPoint, entry_points

import pytest

from oaken.core.clock import FakeClock
from oaken.core.errors import NotFound, PluginError
from oaken.core.events import EventBus, PluginHealthChanged
from oaken.core.settings import AISettings, Settings, SyncSettings
from oaken.plugins.host import PluginHost
from oaken.plugins.tests.conftest import Installer

EMBEDDER = """
from oaken.plugins.base import BaseEmbedder
from oaken.plugins.contracts import Health

class Good(BaseEmbedder):
    model_id = "good"
    dimensions = 4
    version = "9"
    calls = []

    async def start(self, ctx):
        await super().start(ctx)
        Good.calls.append(("start", ctx.name, dict(ctx.config)))

    async def stop(self):
        Good.calls.append(("stop",))

    async def health(self):
        Good.calls.append(("health",))
        return Health(Good.ok if hasattr(Good, "ok") else True, "")

    async def embed(self, texts):
        return [[0.0] * 4 for _ in texts]
"""


def _third_party_only(group: str) -> list[EntryPoint]:
    """The first-party plugins installed in this environment are not under test here."""
    return [ep for ep in entry_points(group=group) if not ep.value.startswith("oaken_")]


def make_host(
    settings: Settings | None = None,
) -> tuple[PluginHost, EventBus, list[PluginHealthChanged]]:
    bus = EventBus()
    seen: list[PluginHealthChanged] = []

    async def record(e: object) -> None:
        assert isinstance(e, PluginHealthChanged)
        seen.append(e)

    bus.subscribe(PluginHealthChanged, record)
    return (
        PluginHost(
            settings or Settings(),
            FakeClock(),
            bus,
            discover=_third_party_only,
            health_interval=3600,
        ),
        bus,
        seen,
    )


async def test_a_plugin_from_a_real_entry_point_is_discovered_started_checked_and_stopped(
    install_plugin: Installer,
) -> None:
    install_plugin("plug_ok", EMBEDDER, [("oaken.embedders", "ollama", "Good")])
    settings = Settings(plugins={"ollama": {"url": "http://localhost:11434"}})
    host, _, seen = make_host(settings)
    await host.start()
    try:
        [st] = host.statuses()
        assert (st.kind, st.name, st.version, st.started, st.healthy) == (
            "embedder",
            "ollama",
            "9",
            True,
            True,
        )
        embedder = host.embedder()  # config selects "ollama" by default
        assert embedder.model_id == "good"
        assert await embedder.embed(["a"]) == [[0.0] * 4]
        await host.check_health()
    finally:
        await host.stop()
    from plug_ok import Good  # type: ignore[import-not-found]

    assert Good.calls == [
        ("start", "ollama", {"url": "http://localhost:11434"}),
        ("health",),
        ("stop",),
    ]
    assert [(e.plugin, e.healthy) for e in seen] == [("embedder:ollama", True)]
    assert not host.statuses()[0].started


async def test_a_plugin_whose_start_raises_is_unhealthy_and_core_carries_on(
    install_plugin: Installer,
) -> None:
    install_plugin(
        "plug_bad",
        """
        from oaken.plugins.base import BaseEmbedder

        class Boom(BaseEmbedder):
            model_id = "x"
            dimensions = 1
            fail = True

            async def start(self, ctx):
                if Boom.fail:
                    raise RuntimeError("cannot reach the service")

            async def embed(self, texts):
                return [[1.0] for _ in texts]
        """,
        [("oaken.embedders", "boom", "Boom")],
    )
    host, _, seen = make_host(Settings(ai=AISettings(embedder="boom")))
    await host.start()
    try:
        assert [(e.plugin, e.healthy) for e in seen] == [("embedder:boom", False)]
        assert "cannot reach the service" in seen[0].detail
        with pytest.raises(PluginError, match="unhealthy"):
            host.embedder()
        from plug_bad import Boom  # type: ignore[import-not-found]

        Boom.fail = False  # the service comes back; the next health pass retries start()
        await host.check_health()
        assert await host.embedder().embed(["a"]) == [[1.0]]
        assert [e.healthy for e in seen] == [False, True]
    finally:
        await host.stop()


async def test_declaring_a_capability_the_class_does_not_implement_is_rejected_at_load(
    install_plugin: Installer,
) -> None:
    install_plugin(
        "plug_liar",
        """
        from oaken.plugins.base import BaseProvider

        class Liar(BaseProvider):
            capabilities = frozenset({"sync.pull", "flags.write"})

            async def pull(self, cursor):
                if False:
                    yield
        """,
        [("oaken.providers", "liar", "Liar")],
    )
    host, _, seen = make_host()
    await host.start()
    try:
        [st] = host.statuses()
        assert not st.healthy and not st.started
        assert "declares 'flags.write' but does not implement set_flags()" in st.detail
        assert [e.healthy for e in seen] == [False]
        assert host.providers_with("flags.write") == []
    finally:
        await host.stop()


async def test_every_problem_is_reported_at_once_and_load_errors_never_raise(
    install_plugin: Installer,
) -> None:
    install_plugin(
        "plug_many",
        """
        from oaken.plugins.base import BaseEmbedder

        class Many(BaseEmbedder):
            api_version = 99
            capabilities = frozenset({"telepathy"})
        """,
        [("oaken.embedders", "many", "Many"), ("oaken.providers", "gone", "Missing")],
    )
    host, _, _ = make_host()
    await host.start()
    try:
        by_name = {s.name: s for s in host.statuses()}
        detail = by_name["many"].detail
        assert "api_version is 99" in detail and "unknown capability 'telepathy'" in detail
        assert "model_id must be a non-empty string" in detail
        assert "embed() is not implemented" in detail
        assert "failed to load" in by_name["gone"].detail
    finally:
        await host.stop()


async def test_health_events_fire_only_on_change(install_plugin: Installer) -> None:
    install_plugin("plug_flap", EMBEDDER, [("oaken.embedders", "ollama", "Good")])
    host, _, seen = make_host()
    await host.start()
    try:
        from plug_flap import Good  # type: ignore[import-not-found]

        await host.check_health()
        await host.check_health()
        assert len(seen) == 1
        Good.ok = False
        await host.check_health()
        Good.ok = True
        await host.check_health()
        assert [e.healthy for e in seen] == [True, False, True]
    finally:
        await host.stop()


PROVIDERS = """
from oaken.plugins.base import BaseProvider

class Full(BaseProvider):
    version = "1"
    async def authenticate(self, ctx): ...
    async def set_flags(self, provider_ids, add, remove): ...
    async def send(self, raw_bytes): return "sent"
    async def pull(self, cursor):
        yield {"messages": [], "next_cursor": "1"}

class ReadOnly(BaseProvider):
    async def pull(self, cursor):
        yield {"next_cursor": "1"}
"""


async def test_capability_queries_and_sending_is_never_routed(install_plugin: Installer) -> None:
    install_plugin(
        "plug_prov",
        PROVIDERS,
        [("oaken.providers", "full", "Full"), ("oaken.providers", "readonly", "ReadOnly")],
    )
    host, _, _ = make_host(Settings(sync=SyncSettings(provider="full")))
    await host.start()
    try:
        names = {s.name: s.capabilities for s in host.statuses() if s.kind == "provider"}
        assert names["full"] == {"auth", "sync.pull", "flags.write", "send"}
        assert names["readonly"] == {"sync.pull"}
        assert len(host.providers_with("sync.pull")) == 2
        assert len(host.providers_with("flags.write")) == 1
        assert host.providers_with("send") == []  # declared, never routed in v1
        assert host.provider().__class__.__name__ == "Full"
        with pytest.raises(NotFound, match="found: full, readonly"):
            host.provider("imap")
    finally:
        await host.stop()


async def test_enrichers_a_decorated_function_and_a_plain_duck_typed_class(
    install_plugin: Installer,
) -> None:
    install_plugin(
        "plug_enr",
        """
        from pydantic import BaseModel
        from oaken.plugins.base import enricher

        class Out(BaseModel):
            words: int

        @enricher("count", output=Out, version="3")
        async def count(msg):
            return {"words": len(msg.body.unwrap().split())}

        class Duck:  # no base class, no start/stop/health
            version = "2"
            needs_llm = False
            output_schema = {}
            async def enrich(self, msg):
                return {"ok": True}
        """,
        [("oaken.enrichers", "count", "count"), ("oaken.enrichers", "duck", "Duck")],
    )
    host, _, _ = make_host()
    await host.start()
    try:
        by_name = {s.name: s for s in host.statuses()}
        assert by_name["count"].version == "3" and by_name["duck"].healthy
        assert len(host.enrichers()) == 2
    finally:
        await host.stop()


async def test_unknown_names_and_missing_plugins(install_plugin: Installer) -> None:
    host, _, _ = make_host()
    await host.start()
    try:
        assert host.statuses() == []
        with pytest.raises(NotFound, match="none installed"):
            host.embedder()
        with pytest.raises(PluginError, match="give the name"):
            host.get("enricher")
    finally:
        await host.stop()


async def test_the_background_health_loop_polls_and_stops_cleanly(
    install_plugin: Installer,
) -> None:
    install_plugin("plug_loop", EMBEDDER, [("oaken.embedders", "ollama", "Good")])
    host = PluginHost(Settings(), FakeClock(), health_interval=0.01)
    await host.start()
    from plug_loop import Good  # type: ignore[import-not-found]

    for _ in range(200):
        if Good.calls.count(("health",)) >= 2:
            break
        await asyncio.sleep(0.01)
    await host.stop()
    assert Good.calls.count(("health",)) >= 2 and Good.calls[-1] == ("stop",)
