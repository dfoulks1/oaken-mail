# SPDX-License-Identifier: Apache-2.0
"""Be accepting in what a plugin returns, strict in what core passes on.

A plugin may return the contract's dataclasses, plain dicts, lists or tuples; these helpers
coerce the result into the frozen contract types and check what the types cannot (vector
counts, finite numbers, JSON-serializable output). A problem raises ``PluginError`` whose message
names the plugin and the call and says what was wrong, so the plugin author can fix it. Nothing
here kills core: callers log the error and fail the one job.
"""

import dataclasses
import inspect
import json
import math
from collections.abc import AsyncIterator
from datetime import UTC
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from oaken.core.errors import PluginError
from oaken.plugins.contracts import (
    EnrichOutput,
    Hit,
    LLMResponse,
    PullBatch,
    RawMessage,
    VectorStats,
)

_BATCH = TypeAdapter(PullBatch)
_HITS = TypeAdapter(list[Hit])
_VECTORS = TypeAdapter(list[list[float]])
_RESPONSE = TypeAdapter(LLMResponse)
_STATS = TypeAdapter(VectorStats)


def _problem(who: str, call: str, exc: ValidationError) -> PluginError:
    parts = [f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}" for e in exc.errors()]
    return PluginError(f"{who}: {call} returned something invalid ({'; '.join(parts[:5])})")


def _utc(msg: RawMessage) -> RawMessage:
    d = msg.internal_date
    if d is not None and d.tzinfo is None:
        return dataclasses.replace(msg, internal_date=d.replace(tzinfo=UTC))
    return msg


def to_batch(obj: object, who: str) -> PullBatch:
    """A ``PullBatch``, from a batch, a dict, or a bare list of messages."""
    if isinstance(obj, list | tuple):
        obj = {"messages": obj}
    try:
        batch = _BATCH.validate_python(obj)
    except ValidationError as exc:
        raise _problem(who, "pull()", exc) from exc
    if batch.next_cursor is not None and not batch.next_cursor:
        raise PluginError(f"{who}: pull() returned an empty next_cursor (use None for none)")
    return dataclasses.replace(batch, messages=tuple(_utc(m) for m in batch.messages))


async def iter_pull(
    provider: Any, cursor: str | None, who: str, folder: str | None = None
) -> AsyncIterator[PullBatch]:
    """Batches from ``provider.pull(cursor)``. The plugin may be an async generator, a
    coroutine returning one batch or a list, or a plain list; each is accepted. ``folder`` is
    passed only when set, so a plugin written before it existed keeps working."""
    result = provider.pull(cursor) if folder is None else provider.pull(cursor, folder=folder)
    if inspect.isawaitable(result):
        result = await result
    if hasattr(result, "__aiter__"):
        async for item in result:
            yield to_batch(item, who)
    elif result is None:
        return
    elif isinstance(result, PullBatch | dict) or (
        isinstance(result, list | tuple) and result and isinstance(result[0], RawMessage | dict)
    ):
        yield to_batch(result, who)
    else:
        for item in result:
            yield to_batch(item, who)


def to_vectors(obj: object, texts: int, dimensions: int, who: str) -> list[list[float]]:
    try:
        vectors = _VECTORS.validate_python(obj)
    except ValidationError as exc:
        raise _problem(who, "embed()", exc) from exc
    if len(vectors) != texts:
        raise PluginError(f"{who}: embed() returned {len(vectors)} vectors for {texts} texts")
    for i, v in enumerate(vectors):
        if len(v) != dimensions:
            raise PluginError(
                f"{who}: embed() vector {i} has {len(v)} dimensions, expected {dimensions}"
            )
        if not all(math.isfinite(x) for x in v):
            raise PluginError(f"{who}: embed() vector {i} contains NaN or infinity")
    return vectors


def to_llm_response(obj: object, who: str) -> LLMResponse:
    """An ``LLMResponse``, from one, a dict of its fields, or plain text."""
    if isinstance(obj, str):
        obj = {"text": obj}
    try:
        return _RESPONSE.validate_python(obj)
    except ValidationError as exc:
        raise _problem(who, "generate()", exc) from exc


def to_enrich_output(obj: object, who: str, model: type[BaseModel] | None = None) -> EnrichOutput:
    """An ``EnrichOutput``, from one, a dict, or a pydantic model. If the enricher declares an
    output model the data is validated against it."""
    if isinstance(obj, EnrichOutput):
        obj = obj.data
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    if not isinstance(obj, dict):
        raise PluginError(f"{who}: enrich() must return a dict or model, got {type(obj).__name__}")
    if model is not None:
        try:
            obj = model.model_validate(obj).model_dump(mode="json")
        except ValidationError as exc:
            raise _problem(who, "enrich()", exc) from exc
    try:
        json.dumps(obj)
    except (TypeError, ValueError) as exc:
        raise PluginError(f"{who}: enrich() result is not JSON-serializable ({exc})") from exc
    return EnrichOutput(data=obj)


def to_hits(obj: object, k: int, who: str) -> list[Hit]:
    try:
        hits = _HITS.validate_python(obj)
    except ValidationError as exc:
        raise _problem(who, "query()", exc) from exc
    if len(hits) > k:
        raise PluginError(f"{who}: query() returned {len(hits)} hits for k={k}")
    scores = [h.score for h in hits]
    if scores != sorted(scores, reverse=True):
        raise PluginError(f"{who}: query() hits are not ordered best first")
    return hits


def to_stats(obj: object, who: str) -> VectorStats:
    if isinstance(obj, int):
        obj = {"count": obj}
    try:
        return _STATS.validate_python(obj)
    except ValidationError as exc:
        raise _problem(who, "stats()", exc) from exc
