# 26 — Sieve management in the UI (and a write path)

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 06c, 13, 16, 17, 21, 22.

## Goal

Manage the user's Sieve scripts from a client: list them, edit one, **check** (compile) it,
**test** it against a message (dry-run), and **save** it so it persists and takes effect on the
next filter run. Today the read/validate side exists (`sieve_list`, `sieve_check`, `sieve_test`);
there is **no write path**. This prompt adds one, carefully, honoring the standing rule that
**writing Sieve is never exposed over MCP**.

## Decision to make in this prompt (record an ADR)

- **Where scripts live and how they are written.** Scripts are `*.sieve` files in the sieve folder
  next to `config.toml`, run in name order (prompt 06c). The write path must do a scoped, safe
  write of a single named script (atomic replace, preserve the others), and must **compile before
  it persists** so an invalid script can never be saved into the run set. Decide: is a saved-but-
  invalid script ever allowed (e.g. disabled), or is save refused unless it compiles? Record the
  choice and the on-disk safety (owner-only, no partial writes).
- **Disposition precedence** stays as decided: a non-Sieve disposition beats a script's; `discarded`
  mail is hidden from default search only, not from MCP. The UI must not imply otherwise.

## Requirements (operations first)

1. **`sieve_save(name, text)`** — `mutate`, confirm-gated, audited, **`mcp="never"`**. Compiles the
   script first (reusing `sieve_check`); refuses to persist on a compile error (returning the error
   + line). Writes `<name>.sieve` atomically into the sieve folder, owner-only. Audit records the
   name and a content hash, never mail.
2. **`sieve_delete(name)`** — `mutate`, confirm-gated, audited, `mcp="never"`. Removes one script.
3. **Keep the read/validate ops** (`sieve_list`, `sieve_check`, `sieve_test`) as the load/test side;
   confirm they return what an editor needs (script text or a way to fetch it, required extensions,
   skipped-with-reason). Add a `sieve_get(name)` if listing does not return text.
4. **Not over MCP.** A test proves `sieve_save`/`sieve_delete` are `mcp="never"` and absent from the
   MCP tool set, while the read/test ops may be exposed. This is the explicit honoring of the
   standing decision.
5. **Order within the editor:** load (`sieve_get`/`list`) → edit → **Check** (`sieve_check`,
   compile errors with line) → **Test** (`sieve_test` on a chosen message, shows each effect and
   the rule that caused it, stores nothing) → **Save** (`sieve_save`, persists; takes effect next
   filter run). A "refilter now" affordance may enqueue the existing refilter job.
6. **CLI** parity: `oaken sieve list|get|check|test|save|delete …`; update 17's table.
7. **UI**: a Sieve page listing scripts (and skipped ones with reasons), an editor with
   Check/Test/Save, and a clear note that writing Sieve is a local, confirmed, audited action that
   is not available to AI tools.

## Acceptance criteria

- `sieve_save` refuses an uncompilable script (error + line, nothing written) and persists a valid
  one atomically without disturbing other scripts; `sieve_delete` removes exactly one.
- A test proves the write ops are `mcp="never"` and not in the MCP tool set, while read/test ops
  behave as before.
- The full list→edit→check→test→save flow is drivable from the CLI and the UI against the same
  operations; the parity test from 22 covers it.
- On-disk safety: scripts are owner-only, writes are atomic (no partial file on failure); a test
  checks modes/atomicity where the platform allows.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green; ADR added.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
