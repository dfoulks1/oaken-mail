# 13 — Operation registry, permission classes, confirm gate

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 09, 12.

## Goal

Define every user-facing capability **once**, as a typed operation. The HTTP API,
CLI and the MCP server are generated from this registry, so they cannot drift apart. The
safety rules (permission classes, confirm gate) live here, in code.

## Requirements

1. **`Operation` definition** (`core/operations/registry.py`): `name`, `summary`,
   pydantic `Input` and `Output` models, `permission: Literal["read","mutate","send"]`,
   `async handler(ctx, input) -> output`, `streaming: bool` (for long ops returning a
   job/event stream), `mcp_exposed: bool`, `marks_read: bool` (declared, asserted by tests).
   JSON Schema and OpenAPI fragments are derived from the models — never hand-written.
2. **Initial operations** (implement over what exists; stub with clear `NotImplemented`
   only where later prompts fill in): `status`, `pipeline_status`, `usage_summary`,
   `sync_now`, `list_messages`, `get_message` (cleaned text + metadata; **never**
   marks read), `get_raw` (headers/raw for inspection), `search_keyword`, `search`
   (hybrid; delegates to 15), `ask` (15), `summarize_message` (marks read, reason per
   caller), `message_presented/dismissed`, `mark_read/unread`, `undo`, `reprocess`,
   `rebuild_index`, `list_plugins`, `plugin_health`. Names are verbs/nouns, stable, documented.
3. **Permission classes + confirm gate (enforced in code):**
   - `read`: always allowed.
   - `mutate`: requires a `Confirmation` object produced by the *caller class*:
     human-facing clients (CLI/UI) can pre-confirm per user action; **MCP calls get a
     two-phase flow** — first call returns a `proposal` (what would change, with ids and
     before/after) and a short-lived single-use `confirmation_token`; only a second call
     presenting the token executes. Tokens are bound to the exact input hash.
   - `send`: defined but **disabled in v1**; registering a `send` operation as
     `mcp_exposed=True` raises at import time (test asserts it).
4. **Caller context** (`CallerContext`): `client ∈ {cli, ui, mcp, internal}`, `actor`
   string, request id. Every mutating execution writes an audit row (reuse 12).
5. **Bulk-read guard:** operations returning many message bodies (`list_messages`
   with bodies, MCP `search`) clamp counts and total characters by config, and return
   *snippets/summary-stored text* by default — token thrift.
6. **In-process client** (`core/operations/client.py`): `CoreClient` that calls the
   registry directly; this is the test harness for everything above, and later what the
   HTTP layer wraps.

## Acceptance criteria

- Registry introspection test: every operation has Input/Output models, a permission
  class, and a generated JSON Schema; names are unique; `mcp_exposed ∧ send` is impossible.
- An MCP-class caller invoking a `mutate` op without a token gets a proposal and no
  side effects; replaying a token, altering the input, or using an expired token fails.
- `get_message`/`search*`/`list_messages` never change read state (asserted by the
  regression test from 12 run through the registry).
- Output size clamps verified on a 500-message corpus.
- OpenAPI document generated from the registry validates (`openapi-spec-validator`).

## Out of scope

Transport (16–18), real hybrid search and Q&A bodies (15), enrichers (14).

## Docs site

Register `docs/tools/gen_operations.py`, generating a *Reference → Operations* page from the
registry (name, summary, permission class, `mcp_exposed`, `marks_read`, input/output
schema). `check_docs.py` must pass.

## Checkpoint

Standard report. Include the full operation table (name, permission, mcp_exposed,
marks_read) for the user to review before the transports are built.
