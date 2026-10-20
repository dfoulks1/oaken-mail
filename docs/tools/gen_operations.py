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
        "A plugin may offer more, named `<plugin>.<name>`. They are listed by `list_operations`"
        " at run time, and are always off over MCP until the user turns them on.",
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
