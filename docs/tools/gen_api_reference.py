# SPDX-License-Identifier: Apache-2.0
"""Reference -> HTTP API, generated from the OpenAPI document the daemon serves.

It imports the package, like the operations page: the document is built by the same function that
answers ``GET /v1/openapi.json``, so the page cannot describe a route the server does not have.
Run it with ``uv run`` so ``oaken`` is importable.
"""

import json
from typing import Any

import generated
from model import BuildError, Config, Page, page_from_meta

PAGE_ID = "reference/api"
METHODS = ("get", "post")


def _fields(schema: dict[str, Any]) -> str:
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    if not props:
        return "none"
    return ", ".join(f"`{n}`" + ("" if n in required else "?") for n in props)


def _body_schema(op: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any] | None:
    content = op.get("requestBody", {}).get("content", {}).get("application/json", {})
    return _resolve(content.get("schema"), doc) if content else None


def _resolve(schema: dict[str, Any] | None, doc: dict[str, Any]) -> dict[str, Any] | None:
    if schema and "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        found: dict[str, Any] | None = doc.get("components", {}).get("schemas", {}).get(name)
        return found
    return schema


def _ok_schema(op: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any] | None:
    ok = op.get("responses", {}).get("200", {}).get("content", {})
    first: dict[str, Any] = next(iter(ok.values()), {})
    return _resolve(first.get("schema"), doc)


INTRO = """\
The local API core serves on `127.0.0.1`. The CLI and the web UI use it, and the OpenAPI document
this page is built from is served at `GET /v1/openapi.json`.
[How it is kept safe](architecture/daemon.md).

## Reaching it

- **Where.** `127.0.0.1` and the port in `runtime.json` (`[api] port`, 0 for a free one).
- **Auth.** `Authorization: Bearer <token>`, the token in `api_token` in the config directory.
  Only `GET /v1/health` is open, and it returns `{"status": "ok"}`. Agent routes take an agent
  token (`oaken_agent_...`) instead, and no other route accepts one.
- **Host.** Must be `127.0.0.1:<port>` or `localhost:<port>`, else `400`.
- **Origin.** A `POST` from another origin, or with `Sec-Fetch-Site: cross-site`, is `403`.
  There are no CORS headers unless one development origin is configured.
- **Content type.** A `POST` must be `application/json`, else `415`.
- **Limits.** Body size (`413`), request rate on mutating calls (`429`), concurrent calls (`429`).
- **Caller class.** `X-Oaken-Client: cli` (the default), `ui` or `mcp`. It can lower privilege and
  never raise it: `mcp` makes the call an agent's, so it cannot confirm by saying so. Anything else
  is `400`.
- **Errors.** `{"code": "...", "message": "..."}`. Messages never contain mail text.

## Routes
"""

CALLING = """\
`status` is `ok` when the operation ran, or `confirmation_required` for a mutation that was not
confirmed: nothing has changed, and `proposal` says what would, with a `confirmation_token`.
A person at the CLI or UI may send `confirm: true`. An agent calls again with the token.
Status codes: `422` input or result does not validate, `403` not permitted, `404` unknown,
`409` conflict, `429` quota, `502` a plugin or provider failed.
"""

EVENTS = """\
`GET /v1/events` is server-sent events. Each frame has an `id` (a number), an `event` (the type
below) and `data` (one line of JSON: ids and counts, never message text). Send `Last-Event-ID` to
resume from the in-memory ring. If that id is older than the ring, or from before core restarted, a
`gap` event comes first, meaning fetch state again. A quiet stream sends `: heartbeat` comments.
"""

MCP_WORDS = {"on": "on by default", "off": "off until turned on", "never": "never"}


def _auth(op: dict[str, Any]) -> str:
    sec = op.get("security")
    if sec == []:
        return "none"
    return "agent token" if sec and "AgentToken" in sec[0] else "API token"


def build(cfg: Config) -> Page:
    try:
        from oaken.api.openapi import build_api_openapi, event_types
        from oaken.core.operations.builtin import core_registry
    except ImportError as exc:
        raise BuildError(f"API generator: cannot import the API: {exc}") from exc
    ops = core_registry().core()
    doc = build_api_openapi(ops, version="")
    json.dumps(doc)  # the document must serialise, or the server could not send it
    paths: dict[str, Any] = doc["paths"]
    fixed = {p: i for p, i in paths.items() if not p.startswith("/v1/operations/")}

    lines = [INTRO, "| Method | Path | Auth | What it does |", "|---|---|---|---|"]
    for path, item in fixed.items():
        for method in METHODS:
            if method in item:
                op = item[method]
                lines.append(
                    f"| {method.upper()} | `{path}` | {_auth(op)} | {op.get('summary', '')} |"
                )
    lines.append(
        f"| POST | `/v1/operations/{{name}}` | API token | Call one operation. {len(ops)} exist. |"
    )
    call = next(i for p, i in paths.items() if p.startswith("/v1/operations/"))["post"]
    body, out = _body_schema(call, doc), _ok_schema(call, doc)
    lines += [
        "",
        "## Calling an operation",
        "",
        f"`POST /v1/operations/{{name}}` takes `{_fields(body or {})}` and answers"
        f" `{_fields(out or {})}`.",
        "",
        CALLING,
        "| Operation | Permission | MCP | Route |",
        "|---|---|---|---|",
    ]
    for op in ops:
        route = f"`POST /v1/operations/{op.name}`"
        lines.append(f"| `{op.name}` | {op.permission} | {MCP_WORDS[op.mcp]} | {route} |")
    lines += [
        "",
        "Each operation's input and output are in [Reference: Operations](operations.html),"
        " and as JSON Schema in the OpenAPI document and in `GET /v1/operations`.",
        "",
        "## Events",
        "",
        EVENTS,
        "| Event type | Meaning | Fields |",
        "|---|---|---|",
    ]
    for ev in event_types():
        names = ", ".join(f"`{f['name']}`" for f in ev["fields"]) or "none"
        lines.append(f"| `{ev['name']}` | {ev['description']} | {names} |")
    lines.append(
        "| `gap` | That id is no longer available; fetch state again. | `first_available` |"
    )
    lines += [
        "",
        "## Agent job routes",
        "",
        "Authenticated by an agent token, never by the API token."
        " See [the swarm](architecture/swarm.md).",
        "",
    ]
    for path, item in fixed.items():
        if path.startswith("/v1/agent/"):
            op = item["post"]
            req, resp = _body_schema(op, doc), _ok_schema(op, doc)
            lines += [
                f"### `POST {path}`",
                "",
                op.get("summary", ""),
                "",
                f"- Request: {_fields(req or {})}",
                f"- Response: {_fields(resp or {})}",
                "",
            ]
    return page_from_meta(
        PAGE_ID,
        "HTTP API",
        "\n".join(lines),
        {
            "subtitle": "HTTP API",
            "lead": "The loopback API: authentication, the Host and Origin rules, every operation"
            " route, the event stream and the agent job routes, from the OpenAPI document.",
            "badges": [["text-bg-secondary", "Generated"]],
            "group": "reference",
            "order": 30,
            "description": "The local HTTP API: auth, Host and Origin rules, routes, events.",
        },
        None,
        False,
    )


generated.register(PAGE_ID, build)
