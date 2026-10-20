# 18 — MCP stdio shim (MCP first, not its own stack)

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 16.

## Goal

`oaken mcp` — a tiny stdio MCP server that forwards tool calls to the running core
over the local API. Its tool list is **generated from the operation registry**. This
replaces the legacy `mailmind_mcp` package; nothing from it is carried over.

## Requirements

1. **Transport:** stdio via the official `mcp` Python SDK. On start it locates core
   exactly like the CLI (runtime file + token). If core isn't running, every tool call
   returns a clear MCP error (`core_not_running`, with the start hint) — the shim never
   starts core and never opens the databases.
2. **Tool generation:** at startup, fetch `/v1/ops`, expose only operations with
   `mcp_exposed=True`. Tool name, description and input schema come from the registry.
   If the registry changes, a restart of the shim picks it up (no code change).
3. **Calls header:** every forwarded request sends `X-Oaken-Client: mcp`, which forces
   the **two-phase confirm flow** for any `mutate` operation: the tool result contains
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

- Tests drive the shim with the SDK's in-memory client against a core booted in-process
  with fakes: tool list equals the exposed subset of the registry; a mutate call returns a
  proposal, then succeeds only with a valid token for the exact same input.
- Search through MCP leaves read state untouched; `summarize_message` marks read once.
- Core down → structured MCP error, no traceback, no hang (timeouts enforced).
- A prompt-injection fixture message returned through `get_message` is wrapped in the
  untrusted envelope in the MCP result.
- Adding a dummy operation to the registry in a test makes it appear as an MCP tool with
  zero shim code changes.

## Out of scope

Streamable-HTTP MCP hosted by core (possible later), MCP resources/prompts, send.

## Docs site

Publish `docs/mcp.md` as a *Start* page (connect Claude Desktop / ollmcp) with the
generated exposed-tool table, the two-phase confirm explanation and the privacy note.
`check_docs.py` must pass.

## Checkpoint

Standard report. Include the exposed-tool table and a transcript of a two-phase
mutate against the fakes.
