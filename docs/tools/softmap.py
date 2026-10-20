# SPDX-License-Identifier: Apache-2.0
"""The software map: a generated page showing what is built and how the pieces layer.

The data lives in docs/softmap.toml. Building the page fails when the data contradicts the
repository or the architecture, so the map cannot quietly drift:

* ids are unique and every ``uses`` target exists;
* a call goes to the same layer or a lower one, never up;
* ``built`` and ``partial`` components have a ``path`` that exists, ``planned`` ones do not.
"""

import html
import tomllib
from typing import Any

import generated
from model import BuildError, Config, Page, page_from_meta

STATUSES = {
    "built": ("text-bg-success", "Built"),
    "partial": ("text-bg-warning", "Partial"),
    "planned": ("text-bg-secondary", "Planned"),
}
CLASS_DEFS = [
    "classDef built stroke:#2e7d32,stroke-width:3px",
    "classDef partial stroke:#b26a00,stroke-width:3px,stroke-dasharray:6 3",
    "classDef planned stroke:#888888,stroke-width:1.5px,stroke-dasharray:3 3",
]


def load(cfg: Config) -> dict[str, Any]:
    path = cfg.docs_dir / "softmap.toml"
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BuildError(f"{path}: {exc}") from exc
    return data


def validate(cfg: Config, data: dict[str, Any]) -> None:
    layers = [layer["id"] for layer in data.get("layers", [])]
    rank = {lid: i for i, lid in enumerate(layers)}  # 0 is the top
    comps = {}
    errors: list[str] = []
    for c in data.get("components", []):
        cid = c.get("id", "?")
        if cid in comps:
            errors.append(f"duplicate component id {cid!r}")
        comps[cid] = c
        for key in ("name", "layer", "status", "prompt", "summary"):
            if key not in c:
                errors.append(f"{cid}: missing '{key}'")
        if c.get("layer") not in rank:
            errors.append(f"{cid}: unknown layer {c.get('layer')!r}")
        if c.get("status") not in STATUSES:
            errors.append(f"{cid}: status must be one of {sorted(STATUSES)}")
    for cid, c in comps.items():
        for target in c.get("uses", []):
            if target not in comps:
                errors.append(f"{cid}: uses unknown component {target!r}")
            elif rank.get(comps[target].get("layer"), 0) < rank.get(c.get("layer"), 0):
                errors.append(f"layering: {cid} calls up into {target}")
        path = c.get("path")
        exists = bool(path) and (cfg.repo_root / path).exists()
        if c.get("status") in ("built", "partial") and not exists:
            errors.append(f"{cid}: marked {c['status']} but path {path!r} does not exist")
        if c.get("status") == "planned" and path and exists:
            errors.append(f"{cid}: marked planned but {path!r} already exists; update its status")
    if errors:
        raise BuildError("softmap.toml: " + "; ".join(errors))


def _cell(text: str) -> str:
    return html.escape(text, quote=False).replace("|", "&#124;")


def _badge(status: str) -> str:
    cls, label = STATUSES[status]
    return f'<span class="badge {cls}">{label}</span>'


def _diagram(data: dict[str, Any]) -> str:
    lines = ["flowchart TB"]
    for layer in data["layers"]:
        members = [c for c in data["components"] if c["layer"] == layer["id"]]
        if not members:
            continue
        lines.append(f'    subgraph L_{layer["id"]}["{layer["name"]}"]')
        lines.append("        direction LR")
        for c in members:
            lines.append(f'        {c["id"]}["{c["name"]}"]:::{c["status"]}')
        lines.append("    end")
    # Invisible links stack the layers top to bottom; the calls are in the tables below.
    firsts = [
        next(c["id"] for c in data["components"] if c["layer"] == layer["id"])
        for layer in data["layers"]
        if any(c["layer"] == layer["id"] for c in data["components"])
    ]
    lines += [f"    {a} ~~~ {b}" for a, b in zip(firsts, firsts[1:], strict=False)]
    lines += [f"    {d}" for d in CLASS_DEFS]
    return "\n".join(lines)


def _builder(cfg: Config) -> Page:
    data = load(cfg)
    validate(cfg, data)
    comps = data["components"]
    names = {c["id"]: c["name"] for c in comps}
    counts = {s: sum(1 for c in comps if c["status"] == s) for s in STATUSES}

    out = [
        "## Status at a glance",
        "",
        f"{len(comps)} components in {len(data['layers'])} layers: "
        f"**{counts['built']} built**, **{counts['partial']} partial**, "
        f"**{counts['planned']} planned**.",
        "",
        "| Status | Meaning |",
        "|---|---|",
        f"| {_badge('built')} | Implemented and checked in the repository. |",
        f"| {_badge('partial')} | Exists, but only part of what is planned. |",
        f"| {_badge('planned')} | Designed; no code yet. |",
        "",
        "## Layout",
        "",
        "Layers run from the things you touch at the top to the foundations at the bottom. A "
        "component calls into its own layer or a lower one, never up; the tables below list "
        "what each one calls. The border shows status: solid green is built, dashed amber is "
        "partial, dashed grey is planned.",
        "",
        "```mermaid",
        _diagram(data),
        "```",
        "",
        "## Components",
        "",
    ]
    for layer in data["layers"]:
        members = [c for c in comps if c["layer"] == layer["id"]]
        if not members:
            continue
        out += [
            f"### {layer['name']}",
            "",
            "| Component | Status | Prompt | Where | What it does | Calls |",
            "|---|---|---|---|---|---|",
        ]
        for c in members:
            where = f"[`{c['path']}`](repo:{c['path']})" if c.get("path") else "—"
            calls = ", ".join(_cell(names[u]) for u in c.get("uses", [])) or "—"
            out.append(
                f"| **{_cell(c['name'])}** | {_badge(c['status'])} | {_cell(str(c['prompt']))} "
                f"| {where} | {_cell(c['summary'])} | {calls} |"
            )
        out.append("")
    out += [
        "## Keeping it current",
        "",
        "::: callout",
        "Edit `docs/softmap.toml` in the same change that builds or alters a component: set "
        "its `status`, add its `path`, and list what it `uses`. The docs check fails if a "
        "component marked built has no path, if a planned one already has code, or if a call "
        "points up a layer.",
        ":::",
        "",
    ]
    return page_from_meta(
        "architecture/software-map",
        "Software map",
        "\n".join(out),
        {
            "subtitle": "What is built, and how it fits",
            "lead": "Every component, its layer, what it calls and whether it exists yet.",
            "badges": [["text-bg-secondary", "Generated"], ["text-bg-warning", "Pre-alpha"]],
            "group": "concepts",
            "order": 20,
            "description": "The layered map of Oaken Mail's components and their build status.",
        },
        None,
        False,
    )


generated.register("architecture/software-map", _builder)
