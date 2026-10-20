# SPDX-License-Identifier: Apache-2.0
from typing import Any

import pytest
from pydantic import BaseModel

from oaken.core.errors import NotFound
from oaken.plugins.base import BaseEmbedder
from oaken.plugins.contracts import PluginOperation
from oaken.plugins.tests.conftest import Installer
from oaken.plugins.tests.test_host import EMBEDDER, make_host
from oaken.plugins.validate import check_plugin


class In(BaseModel):
    x: int = 0


class Out(BaseModel):
    y: int = 0


async def handler(ctx: Any, inp: Any) -> Out:
    return Out(y=inp.x)


def op(name: str = "echo", **kw: Any) -> PluginOperation:
    base: dict[str, Any] = {
        "name": name,
        "summary": "Echo.",
        "permission": "read",
        "input": In,
        "output": Out,
        "handler": handler,
    }
    return PluginOperation(**{**base, **kw})


class Offers(BaseEmbedder):
    model_id = "m"
    dimensions = 2
    version = "1"

    def __init__(self, ops: Any) -> None:
        self._ops = ops

    def operations(self) -> list[PluginOperation]:
        if isinstance(self._ops, Exception):
            raise self._ops
        return list(self._ops)

    async def embed(self, texts: Any) -> list[list[float]]:
        return [[0.0, 0.0] for _ in texts]


def problems(ops: Any) -> list[str]:
    return check_plugin("embedder", "p", Offers(ops))[1]


def test_good_declarations_have_no_problems() -> None:
    assert problems([op(), op("write", permission="mutate", mcp="never")]) == []


@pytest.mark.parametrize(
    ("ops", "word"),
    [
        ([op("Bad-Name")], "lower-case"),
        ([op("a"), op("a")], "twice"),
        ([op(permission="send")], "cannot send"),
        ([op(mcp="on")], "mcp is off or never"),
        ([op(input=dict)], "pydantic"),
        ([op(output=int)], "pydantic"),
        ([op(handler=None)], "handler"),
        (["nope"], "PluginOperation"),
        (RuntimeError("secret body text"), "operations() failed: RuntimeError"),
    ],
)
def test_bad_declarations_are_named(ops: Any, word: str) -> None:
    found = problems(ops)
    assert any(word in p for p in found), found
    assert not any("secret" in p for p in found)


async def test_only_healthy_running_plugins_offer_operations(install_plugin: Installer) -> None:
    install_plugin(
        "plug_ops",
        EMBEDDER
        + """
from oaken.plugins.contracts import PluginOperation
from pydantic import BaseModel

class I(BaseModel):
    pass

async def h(ctx, inp):
    return I()

class Good2(Good):
    def operations(self):
        return [PluginOperation("ping", "Ping.", "read", I, I, h)]
""",
        [("oaken.embedders", "ollama", "Good2")],
    )
    host, _, _ = make_host()
    await host.start()
    try:
        [offered] = host.offered_operations()
        assert (offered.plugin, offered.declaration.name) == ("ollama", "ping")
        ctx = host.operation_context("ollama", "cli", "me")
        assert (ctx.plugin, ctx.client, ctx.actor) == ("ollama", "cli", "me")
        with pytest.raises(NotFound):
            host.operation_context("other", "cli", "me")
        from plug_ops import Good  # type: ignore[import-not-found]

        Good.ok = False
        await host.check_health()
        assert host.offered_operations() == []
    finally:
        await host.stop()


async def test_config_schema_comes_from_config_model(install_plugin: Installer) -> None:
    install_plugin(
        "plug_cfg",
        EMBEDDER
        + """
from pydantic import BaseModel

class Cfg(BaseModel):
    url: str = "x"

class WithCfg(Good):
    config_model = Cfg
""",
        [("oaken.embedders", "ollama", "WithCfg"), ("oaken.embedders", "plain", "Good")],
    )
    host, _, _ = make_host()
    await host.start()
    try:
        schema = host.config_schema("embedder", "ollama")
        assert schema is not None and "url" in schema["properties"]
        assert host.config_schema("embedder", "plain") is None
        assert host.config_schema("embedder", "missing") is None
    finally:
        await host.stop()
