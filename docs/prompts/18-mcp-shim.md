# 18 — MCP server in core (and the stdio bridge)

**Read first:** `docs/prompts/00-context.md` and `docs/adr/0004-mcp-server-in-core.md`.
**Depends on:** 16.

## Goal

The product's headline feature: a local, data-only MCP server for sensitive mail, **hosted
by core** (not a separate client) and runnable whether or not the web UI is. Its tool list is
**generated from the operation registry**. A small `oaken mcp` stdio bridge lets stdio-only
MCP clients reach it. This replaces the legacy `mailmind_mcp` package; nothing from it is
carried over.

## Requirements

1. **In-core server:** the official `mcp` Python SDK, streamable HTTP transport, mounted at
   `/mcp` on the API listener (prompt 16). Its own token (mode `0600`), narrower than the
   general API token; Host and Origin checks apply; own concurrency and result-size limits.
   Enabled by `[api] mcp = true`; independent of the web UI. Calls go straight to the
   registry with the caller class `mcp` set by the mount.
1b. **`oaken mcp` stdio bridge:** locates core exactly like the CLI (runtime file + token),
   speaks stdio to the client and forwards to `/mcp`. If core isn't running, every tool call
   returns a clear MCP error (`core_not_running`, with the start hint); the bridge never
   starts core, never opens the databases and adds no behaviour.
2. **Tool generation:** at startup the server reads the registry and exposes only
   operations with `mcp_exposed=True`. Tool name, description and input schema come from the
   registry; no per-tool code.
3. **Caller class:** the `mcp` mount forces the **two-phase confirm flow** for any `mutate`
   operation: the tool result contains
   the `proposal` and a `confirmation_token`, plus text telling the *human* what is about
   to change; the model must re-call the same tool with the token to execute. The shim
   adds no bypass. Document this behavior prominently in the tool descriptions so
   clients know.
4. **Result shaping for token thrift:** MCP results default to compact JSON (ids,
   subject, sender, date, summary/snippet) with a documented `detail` parameter for more;
   bodies clamp to the per-call limits defined in prompt 13. Content that came from
   mail is returned inside the same delimiter/`UntrustedText` envelope the registry uses,
   with a short note field stating it is untrusted data.
5. **Exposure decisions to implement and list in the report:** `search`, `get_message`
   (cleaned text, bounded), `ask`, `list_messages`, `status`, `summarize_message`
   (**marks read, reason `mcp_summarized`; the tool description says so**), `mark_read/unread`
   (two-phase), `undo`. `get_raw` and `rebuild_index`/`reprocess` are **not** exposed.
   There is no send tool, and the registry already makes that impossible.
6. Docs: `docs/mcp.md` with config snippets for Claude Desktop and `ollmcp`, and the
   privacy note: *connecting a remote model to this server sends the returned mail
   excerpts to that model's provider*.

## Acceptance criteria

- Tests drive the server with the SDK's in-memory client against a core booted in-process
  with fakes (web UI not running): tool list equals the exposed subset of the registry; a mutate call returns a
  proposal, then succeeds only with a valid token for the exact same input.
- Search through MCP leaves read state untouched; `summarize_message` marks read once.
- The bridge, spawned as a subprocess, lists and calls tools through to the in-core server;
  core down → structured MCP error, no traceback, no hang (timeouts enforced).
- The MCP token cannot call the general API's mutating routes and vice versa; a request with
  a foreign `Origin` or `Host` is rejected; `[api] mcp = false` removes `/mcp` entirely.
- A burst of slow MCP calls does not delay a concurrent general-API request beyond a bound.
- A prompt-injection fixture message returned through `get_message` is wrapped in the
  untrusted envelope in the MCP result.
- Adding a dummy operation to the registry in a test makes it appear as an MCP tool with
  zero server code changes.

## Out of scope

Streamable-HTTP MCP hosted by core (possible later), MCP resources/prompts, send.

## Docs site

Publish `docs/mcp.md` as a *Start* page (connect Claude Desktop / ollmcp) with the
generated exposed-tool table, the two-phase confirm explanation and the privacy note.
`check_docs.py` must pass.

## Checkpoint

Standard report. Include the exposed-tool table and a transcript of a two-phase
mutate against the fakes.
