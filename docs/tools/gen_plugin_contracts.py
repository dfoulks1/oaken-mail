# SPDX-License-Identifier: Apache-2.0
"""Reference -> Plugin contracts, generated from src/oaken/plugins/contracts.py and base.py.

The source is parsed with ``ast``, never imported, so the docs tooling stays independent of the
runtime package and the page can never say something the code does not. Protocols list their
members with signatures and docstrings, dataclasses their fields, and the capability tables come
from the literal dictionaries in ``contracts.py``.
"""

import ast
from pathlib import Path

import generated
from model import BuildError, Config, Page, page_from_meta

CONTRACTS = Path("src/oaken/plugins/contracts.py")
BASE = Path("src/oaken/plugins/base.py")
PAGE_ID = "reference/plugin-contracts"


def _parse(cfg: Config, rel: Path) -> ast.Module:
    path = cfg.repo_root / rel
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        raise BuildError(f"plugin contracts generator: cannot read {rel}: {exc}") from exc


def _literal(tree: ast.Module, name: str) -> object:
    for node in tree.body:
        value = None
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name:
            value = node.value
        elif isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == name for t in node.targets
        ):
            value = node.value
        if value is not None:
            return ast.literal_eval(value)
    raise BuildError(f"plugin contracts generator: {name} not found in contracts.py")


def _decorators(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    out = set()
    for d in node.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        out.add(ast.unparse(target))
    return out


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = ast.unparse(fn.args)
    ret = f" -> {ast.unparse(fn.returns)}" if fn.returns else ""
    prefix = "async def" if isinstance(fn, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {fn.name}({args}){ret}"


def _doc(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return " ".join((ast.get_docstring(node) or "").split())


def _is_protocol(node: ast.ClassDef) -> bool:
    return any(ast.unparse(b) == "Protocol" for b in node.bases)


def _members(node: ast.ClassDef) -> tuple[list[str], list[ast.FunctionDef | ast.AsyncFunctionDef]]:
    attrs = [ast.unparse(n) for n in node.body if isinstance(n, ast.AnnAssign)]
    fns = [
        n
        for n in node.body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and not n.name.startswith("_")
    ]
    return attrs, fns


def _dataclass_section(node: ast.ClassDef) -> list[str]:
    lines = [f"### `{node.name}`", ""]
    if doc := _doc(node):
        lines += [doc, ""]
    lines += ["| Field | Type | Default |", "|---|---|---|"]
    for n in node.body:
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            default = ast.unparse(n.value) if n.value is not None else "required"
            if default.startswith("field("):
                default = "empty" if "default_factory" in default else default
            kind = ast.unparse(n.annotation).replace("|", "\\|")  # a pipe would split the cell
            lines.append(f"| `{n.target.id}` | `{kind}` | `{default}` |")
    return [*lines, ""]


def _protocol_section(node: ast.ClassDef) -> list[str]:
    attrs, fns = _members(node)
    lines = [f"### `{node.name}`", ""]
    if doc := _doc(node):
        lines += [doc, ""]
    if attrs:
        lines += ["Attributes:", "", *[f"- `{a}`" for a in attrs], ""]
    for fn in fns:
        lines += ["```python", _signature(fn), "```", ""]
        if d := _doc(fn):
            lines += [d, ""]
    return lines


def _base_section(node: ast.ClassDef) -> list[str]:
    attrs, fns = _members(node)
    placeholders = [f for f in fns if "_default" in _decorators(f)]
    lines = [f"### `{node.name}`", ""]
    if doc := _doc(node):
        lines += [doc, ""]
    if attrs:
        lines += ["Set:", "", *[f"- `{a}`" for a in attrs], ""]
    if placeholders:
        lines += ["Override:", "", *[f"- `{_signature(f)}`" for f in placeholders], ""]
    return lines


def build(cfg: Config) -> Page:
    contracts = _parse(cfg, CONTRACTS)
    base = _parse(cfg, BASE)
    version = _literal(contracts, "PLUGIN_API_VERSION")
    groups = _literal(contracts, "ENTRY_POINT_GROUPS")
    caps = _literal(contracts, "CAPABILITIES")
    required = _literal(contracts, "REQUIRED_MEMBERS")
    assert isinstance(groups, dict) and isinstance(caps, dict) and isinstance(required, dict)  # noqa: S101

    classes = [n for n in contracts.body if isinstance(n, ast.ClassDef)]
    lines = [
        "This page is generated from `src/oaken/plugins/contracts.py` and",
        "`src/oaken/plugins/base.py`, so it always matches the code. For a walkthrough, see",
        "[Writing a plugin](../plugins.html).",
        "",
        f"The plugin API version is **{version}**. A plugin whose `api_version` differs is"
        " rejected at load.",
        "",
        "## Kinds, entry points and capabilities",
        "",
        "| Kind | Entry-point group | Capabilities (and the method each needs)"
        " | Required members |",
        "|---|---|---|---|",
    ]
    for kind, group in groups.items():
        cap_text = ", ".join(f"`{c}` ({m}())" for c, m in caps[kind].items()) or "none"
        req = ", ".join(f"`{r}`" for r in required[kind]) or "none"
        lines.append(f"| `{kind}` | `{group}` | {cap_text} | {req} |")
    lines += [
        "",
        "## Data types",
        "",
        "Frozen dataclasses of plain data. These cross the boundary.",
        "",
    ]
    for node in classes:
        if "dataclass" in {d.split("(")[0] for d in _decorators(node)}:
            lines += _dataclass_section(node)
    lines += ["## Protocols", "", "A plugin satisfies a contract by having these members.", ""]
    for node in classes:
        if _is_protocol(node):
            lines += _protocol_section(node)
    lines += [
        "## Base classes",
        "",
        "Optional. They supply defaults so a plugin overrides only what it does.",
        "",
    ]
    for stmt in base.body:
        if isinstance(stmt, ast.ClassDef) and stmt.name.startswith("Base"):
            lines += _base_section(stmt)
    for stmt in base.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name == "enricher":
            lines += [
                "### The `@enricher` decorator",
                "",
                "```python",
                _signature(stmt),
                "```",
                "",
                _doc(stmt),
                "",
            ]

    return page_from_meta(
        PAGE_ID,
        "Plugin contracts",
        "\n".join(lines),
        {
            "subtitle": "Plugin contracts",
            "lead": "The signatures, data types and capabilities of the five plugin kinds,"
            " generated from the code.",
            "badges": [["text-bg-secondary", "Generated"]],
            "group": "reference",
            "order": 10,
            "description": "Every plugin contract: Protocol methods, data types, capabilities"
            " and base classes.",
        },
        None,
        False,
    )


generated.register(PAGE_ID, build)
