# SPDX-License-Identifier: Apache-2.0
"""Reference -> Operations, generated from the operation registry.

Unlike the plugin-contracts page this one imports the package: an operation is a decorated
function whose models, permission and MCP rules only exist once it is defined, and the registry
refuses to build an operation that breaks a rule. The page therefore cannot list one the code
would not accept. Run it with ``uv run``, so ``oaken`` is importable.
"""

from typing import Any

import generated
from model import BuildError, Config, Page, page_from_meta

PAGE_ID = "reference/operations"
MCP_WORDS = {"on": "on by default", "off": "off until you turn it on", "never": "never"}


def _fields(schema: dict[str, Any]) -> str:
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    if not props:
        return "none"
    return ", ".join(f"`{n}`" + ("" if n in required else "?") for n in props)


def _catalog() -> list[Any]:
    """The plugins installed in this environment, loaded but not started."""
    import asyncio

    try:
        from oaken.core.clock import SystemClock
        from oaken.core.settings import Settings
        from oaken.plugins.host import PluginHost
    except ImportError as exc:
        raise BuildError(f"operations generator: cannot import the plugin host: {exc}") from exc
    host = PluginHost(Settings(), SystemClock(), health_interval=3600)
    asyncio.run(host.load())
    return host.catalog()


def build(cfg: Config) -> Page:
    try:
        from oaken.core.operations.builtin import core_registry
    except ImportError as exc:
        raise BuildError(f"operations generator: cannot import the registry: {exc}") from exc
    ops = core_registry().core()
    lines = [
        "Every operation, defined once. The general API, the CLI and the MCP server are generated"
        " from the same registry. [How it works](architecture/operations.md).",
        "",
        "**Permission**: `read` changes nothing, `mutate` asks for confirmation, `send` is refused"
        " in this version. **MCP** is the ceiling in code and the default; the user can lower it,"
        " and raise it only up to the ceiling. **Marks read** means it may change read state.",
        "",
        "| Operation | Permission | MCP | Marks read | What it does |",
        "|---|---|---|---|---|",
    ]
    for op in ops:
        lines.append(
            f"| `{op.name}` | {op.permission} | {MCP_WORDS[op.mcp]} |"
            f" {'yes' if op.marks_read else 'no'} | {op.summary} |"
        )
    lines += ["", "## Inputs and outputs", "", "A trailing `?` marks an optional field.", ""]
    for op in ops:
        lines += [
            f"### `{op.name}`",
            "",
            op.description or op.summary,
            "",
            f"- Input: {_fields(op.input_schema())}",
            f"- Output: {_fields(op.output_schema())}",
            "",
        ]
    lines += [
        "## Plugin operations",
        "",
        "A plugin may offer more, named `<plugin>.<name>` (a provider's login, say). They are"
        " listed by `list_operations` at run time, are always off over MCP until the user turns"
        " them on, and are reachable from the CLI as `oaken <plugin> <name>` (or"
        " `oaken operation <plugin>.<name>`, with one flag per input field). A plugin declares one"
        " with the `@command` decorator in `oaken.plugins.base`.",
        "",
        "Plugins installed with this version, and the functions each offers. A plugin you install"
        " yourself adds its own, which show up on your machine, not here.",
        "",
        "| Plugin | Kind | Version | Functions |",
        "|---|---|---|---|",
    ]
    catalog = _catalog()
    every = next(
        ([d for d in declared if d.lifecycle] for _k, _n, _v, declared in catalog if declared), []
    )
    for kind, name, version, declared in catalog:
        own = [d for d in declared if not d.lifecycle]
        names = ", ".join(f"`{name}.{d.name}`" for d in own) or "none of its own"
        lines.append(f"| `{name}` | {kind} | {version or '-'} | {names} |")
    lines.append("")
    if every:
        lines += [
            "### Commands every plugin has",
            "",
            "A plugin built on a base class offers these with no code, as `<plugin>.<command>`"
            " (`oaken <plugin> <command>`). Core runs them, they work whatever state the plugin is"
            " in, and none is ever offered over MCP. A plugin may write its own to replace one.",
            "",
            "| Command | Permission | What it does |",
            "|---|---|---|",
        ]
        lines += [f"| `{d.name}` | {d.permission} | {d.summary} |" for d in every]
        lines.append("")
        for d in every:
            lines += [
                f"#### `<plugin>.{d.name}`",
                "",
                f"- Input: {_fields(d.input.model_json_schema())}",
                f"- Output: {_fields(d.output.model_json_schema())}",
                "",
            ]
    for kind, name, _version, declared in catalog:
        for d in declared:
            if d.lifecycle:
                continue
            lines += [
                f"### `{name}.{d.name}`",
                "",
                f"{d.summary} Offered by the `{name}` {kind} plugin; permission `{d.permission}`,"
                f" MCP `{d.mcp}`.",
                "",
                f"- Input: {_fields(d.input.model_json_schema())}",
                f"- Output: {_fields(d.output.model_json_schema())}",
                "",
            ]
    return page_from_meta(
        PAGE_ID,
        "Operations",
        "\n".join(lines),
        {
            "subtitle": "Operations",
            "lead": "Everything a client can ask Oaken Mail to do, with its permission class and"
            " MCP exposure, generated from the registry.",
            "badges": [["text-bg-secondary", "Generated"]],
            "group": "reference",
            "order": 20,
            "description": "Every operation: permission class, MCP exposure, inputs and outputs.",
        },
        None,
        False,
    )


generated.register(PAGE_ID, build)
