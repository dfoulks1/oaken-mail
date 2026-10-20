# SPDX-License-Identifier: Apache-2.0
"""The whole HTTP API as one OpenAPI 3.1 document: the operation routes from the registry
(``core.operations.openapi``) plus the few routes that are not operations. Every schema comes from a
pydantic model, and the event list comes from the event classes, so the page generated from this
(Reference -> HTTP API) cannot drift from the code."""

import dataclasses
import re
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from oaken.api import models
from oaken.core import events
from oaken.core.operations.openapi import REF, build_openapi
from oaken.core.operations.registry import AnyOperation

PROBLEMS = {
    "400": "The Host header is not allowed, or the body is not a JSON object.",
    "401": "No valid bearer token for this route.",
    "403": "The Origin is not allowed, or the caller may not do this.",
    "413": "The body is over the size limit.",
    "415": "A request with a body must be application/json.",
    "422": "The input does not fit.",
    "429": "Too many requests, or the quota is used.",
}


def _describe(cls: type) -> str:
    """The class docstring, or the name as a sentence when it has none (``dataclass`` fills
    ``__doc__`` with the signature in that case)."""
    doc = " ".join((cls.__doc__ or "").split())
    if doc and not doc.startswith(f"{cls.__name__}("):
        return doc
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", cls.__name__).lower()
    return words[:1].upper() + words[1:] + "."


def event_types() -> list[dict[str, Any]]:
    """Every event the stream can carry, with its fields."""
    found: list[type[events.Event]] = []

    def walk(cls: type[events.Event]) -> None:
        for sub in cls.__subclasses__():
            found.append(sub)
            walk(sub)

    walk(events.Event)
    out = []
    for cls in sorted(found, key=lambda c: c.__name__):
        if not cls.__module__.startswith("oaken.core.events") or not dataclasses.is_dataclass(cls):
            continue  # a plugin's own event: it declares itself
        fields = [
            {"name": f.name, "type": str(f.type).replace("oaken.core.ids.", "")}
            for f in dataclasses.fields(cls)
        ]
        out.append(
            {
                "name": cls.__name__,
                "description": _describe(cls),
                "fields": fields,
            }
        )
    return out


def _ref(schemas: dict[str, Any], model: type[BaseModel]) -> dict[str, str]:
    schema = model.model_json_schema(ref_template=REF)
    for name, sub in schema.pop("$defs", {}).items():
        schemas[name] = sub
    schemas[model.__name__] = schema
    return {"$ref": REF.format(model=model.__name__)}


def _problems(*codes: str) -> dict[str, Any]:
    return {
        c: {
            "description": PROBLEMS[c],
            "content": {"application/json": {"schema": {"$ref": REF.format(model="Error")}}},
        }
        for c in codes
    }


def build_api_openapi(operations: Iterable[AnyOperation], *, version: str = "1") -> dict[str, Any]:
    doc = build_openapi(operations, version=version)
    schemas: dict[str, Any] = doc["components"]["schemas"]

    def ok(model: type[BaseModel]) -> dict[str, Any]:
        return {
            "200": {
                "description": "Success.",
                "content": {"application/json": {"schema": _ref(schemas, model)}},
            }
        }

    def body(model: type[BaseModel]) -> dict[str, Any]:
        return {
            "required": True,
            "content": {"application/json": {"schema": _ref(schemas, model)}},
        }

    general: list[dict[str, list[str]]] = [{"ApiToken": []}]
    agent: list[dict[str, list[str]]] = [{"AgentToken": []}]

    for item in doc["paths"].values():
        post = item["post"]
        post["security"] = general
        post["responses"] = {
            **post["responses"],
            **_problems("400", "401", "403", "413", "415", "422", "429"),
        }
        post["responses"].pop("default", None)
        post["parameters"] = [
            {
                "name": "X-Oaken-Client",
                "in": "header",
                "required": False,
                "schema": {"enum": ["cli", "ui", "mcp"]},
                "description": "Lowers the caller class. `mcp` forces the confirm flow.",
            }
        ]
    paths: dict[str, Any] = dict(doc["paths"])
    paths["/v1/health"] = {
        "get": {
            "operationId": "health",
            "summary": "Liveness. Needs no token and reveals nothing else.",
            "security": [],
            "responses": {**ok(models.Health), **_problems("400")},
        }
    }
    paths["/v1/operations"] = {
        "get": {
            "operationId": "list_operations_http",
            "summary": "The operations this caller may call, with their schemas.",
            "security": general,
            "responses": {**ok(models.OperationList), **_problems("400", "401")},
        }
    }
    paths["/v1/openapi.json"] = {
        "get": {
            "operationId": "openapi",
            "summary": "This document.",
            "security": general,
            "responses": {"200": {"description": "OpenAPI 3.1."}, **_problems("400", "401")},
        }
    }
    paths["/v1/events"] = {
        "get": {
            "operationId": "events",
            "summary": "Server-Sent Events: what core does, as it happens.",
            "description": "Each event has an `id` (a counter), an `event` name and one line of "
            "JSON `data`. Send `Last-Event-ID` to replay what the buffer still holds; a `gap` "
            "event says some were lost. A `: heartbeat` comment keeps the connection alive.",
            "security": general,
            "parameters": [
                {
                    "name": "Last-Event-ID",
                    "in": "header",
                    "required": False,
                    "schema": {"type": "string"},
                }
            ],
            "responses": {
                "200": {
                    "description": "An endless text/event-stream.",
                    "content": {"text/event-stream": {"schema": {"type": "string"}}},
                },
                **_problems("400", "401"),
            },
            "x-oaken-events": event_types(),
        }
    }

    def agent_route(
        op_id: str, summary: str, req: type[BaseModel] | None, res: type[BaseModel]
    ) -> dict[str, Any]:
        post: dict[str, Any] = {
            "operationId": op_id,
            "summary": summary,
            "security": agent,
            "responses": {
                **ok(res),
                **_problems("400", "401", "403", "413", "415", "422", "429"),
                "409": {
                    "description": "The agent no longer holds the lease (expired or revoked).",
                    "content": {
                        "application/json": {"schema": {"$ref": REF.format(model="Error")}}
                    },
                },
            },
        }
        if req is not None:
            post["requestBody"] = body(req)
        return {"post": post}

    job_id = {"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}
    paths["/v1/agent/jobs/claim"] = agent_route(
        "agent_claim",
        "Lease the best runnable job of the agent's kinds, or none.",
        models.ClaimRequest,
        models.ClaimResponse,
    )
    for name, summary, req, res in (
        (
            "heartbeat",
            "Extend the lease; says whether the job was cancelled.",
            None,
            models.HeartbeatResponse,
        ),
        (
            "complete",
            "Finish the job with a result that matches its kind's schema.",
            models.CompleteRequest,
            models.Ok,
        ),
        (
            "fail",
            "Give the job back with an error; core decides whether to retry.",
            models.FailRequest,
            models.FailResponse,
        ),
    ):
        route = agent_route(f"agent_{name}", summary, req, res)
        route["post"]["parameters"] = [job_id]
        paths[f"/v1/agent/jobs/{{job_id}}/{name}"] = route
    _ref(schemas, models.Health)

    doc["paths"] = paths
    doc["info"]["description"] = (
        "Loopback only (127.0.0.1). Every route but /v1/health needs `Authorization: Bearer`. "
        "The Host header must be 127.0.0.1:<port> or localhost:<port>; a state-changing request "
        "with a foreign Origin is refused; bodies are application/json."
    )
    doc["servers"] = [{"url": "http://127.0.0.1:{port}", "variables": {"port": {"default": "0"}}}]
    doc["security"] = general
    doc["components"]["securitySchemes"] = {
        "ApiToken": {
            "type": "http",
            "scheme": "bearer",
            "description": "~/.config/oaken/api_token",
        },
        "AgentToken": {
            "type": "http",
            "scheme": "bearer",
            "description": "An agent's own token. Accepted only on /v1/agent/ routes.",
        },
    }
    return doc
