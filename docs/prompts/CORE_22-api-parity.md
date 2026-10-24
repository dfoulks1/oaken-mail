# 22 — API expansion and CLI/UI parity

**Read first:** `docs/prompts/00-context.md` and `docs/prompts/WEBUI_21b-webui-client-foundation.md`
(the function ⇄ operation map and the contract gaps this prompt must close).
**Depends on:** 13, 16, 17, 21, 21b.

## Goal

Make the general API carry **every** call the Web UI and the CLI need, with **1:1 feature parity**
between them — because they are both thin clients of the **same** operation registry. There is one
backend; parity is a property of the architecture, and this prompt turns it into an enforced
contract rather than a hope. It also audits what is missing today and sets the rule the UI-feature
prompts (23–27) follow.

## The rule (applies to 23–29, and to all future UI/CLI work)

Every user-facing capability is a **registry operation first**. A prompt adds, in this order:

1. **The operation(s)** in `src/oaken/core/operations/builtin/…` — typed input/output, a permission
   class (`read`/`mutate`/`send`), and an **MCP ceiling** in code (`mcp="on"|"off"|"never"`), per
   prompt 13. Anything that mutates mail is gated and audited there, not in a client.
2. **HTTP exposure** — automatic, since routes are generated from the registry (prompt 16). No
   per-operation route is written.
3. **CLI reachability** — a named verb when it deserves ergonomics, otherwise confirm the generic
   `oaken op NAME --json '{…}'` fallback already reaches it. A registry change never requires a CLI
   release.
4. **UI wiring** — the browser calls the same `/v1/operations/{name}` through `oaken-webui`.
5. **Docs/map** — `docs/softmap.toml`, the operations reference, and 17's CLI table.

No client ever holds business logic. If the UI needs something the CLI cannot also do, that is a
**bug in this rule**, not a UI feature — add the operation.

## Requirements

1. **Audit** the current registry against both clients and write the result into this prompt's
   report and into the operations reference:
   - list every registered operation with its permission and MCP ceiling;
   - map each CLI command (prompt 17) to its operation(s);
   - map each UI surface (the pages under `apps/webui/oakenmail-templates/`) to the operation(s) it
     needs, and mark which are **missing** from the registry today. Known gaps to record (built by
     23–29, not here): plugin config load/test/save (23); accounts onboarding and receive-only
     (24); per-folder/label **unread counts** and **star** (25); a Sieve **write** path (26);
     **contacts/people** (27); **send, identities** (28); **core settings, storage, cache,
     vacuum, event tail** (29). Note also any read op the UI needs that only exists as a
     CLI convenience, and vice versa.
2. **Parity test.** Add a test that asserts, for a declared set of "UI capabilities", each maps to a
   registered operation that is exposed on the general (non-MCP) surface and reachable by a thin
   client — i.e. the CLI's generic `op` path can drive anything the UI can. The test fails if a new
   UI feature ships without a backing operation. Keep the capability list in one place both clients
   can read.
3. **Operation hygiene for clients.** Confirm `/v1/operations` returns, for each op, enough for a
   client to build itself without importing core: name, summary, permission, `marks_read`,
   `streaming`, MCP exposure, and input/output JSON Schema. Fill any missing field. The UI's forms
   and the CLI's `--help`/`op` fallback are both built from this.
4. **No MCP leakage.** The audit and a test confirm the UI/CLI parity set never includes an
   operation whose only intended surface is MCP. `send` operations (built by 28) are **in** the
   UI/CLI parity set and **never** on MCP (the registry ceiling is unchanged; how MCP and plugin
   capability permissions are managed is a separate discussion, 21b Q6).
5. **Close the read-side contract gaps (21b section B, G1–G4).** These are changes to existing
   operations, not new features, and they are this prompt's real build work:
   - `MessageItem` and `GetMessageOutput` gain `starred` and `location`; `GetMessageOutput` also
     gains `to_addrs`, `cc_addrs`, `message_id_header`, `references` (needed by the reader and by
     reply in 28). Attachments keep `content_type`/`size` (bytes); the client derives icon/format.
   - `list_messages`, `search_keyword` and `search` gain `offset` and return `total` (a count of
     rows matching the filters, not of the page), so the UI can show "a–b of N" and page.
   - `MessageFilter` gains `location`, `starred` and `has_attachment`; `participant` is added by 27.
   - The index query parser gains `to:`, `subject:`, `has:attachment`, `in:`/`location:` (folder) and
     `is:starred`, in the same safe-quoting style as `from:`. This touches prompt 07's code: add
     tests there and say so in the report. The advanced-search form in the UI composes a plain query
     string from these operators; there is no second query language.
   - Fix the `list_labels` and `get_message` fixture shapes in `demo-fixtures.js` (21b C1).
6. **A contract test for the UI.** Add the `FIELDS` table from 21b (the fields each render path
   reads) and a test that every field exists in the operation's output schema.
7. **This prompt adds the contract, the audit and the read-side gaps above, not the missing
   features.** The missing operations are delivered by 23–29, each of which must satisfy the parity
   test for its feature.

## Acceptance criteria

- The audit (operation ⇄ CLI ⇄ UI, with gaps) exists in the report and in the operations reference
  page, and the docs check is green.
- The parity test passes for everything built so far and would fail if a UI capability lacked a
  backing, client-reachable operation (demonstrate with a temporary xfail or a comment showing the
  failure mode).
- `/v1/operations` carries the full per-op metadata a client needs; a test checks the required
  fields are present for every operation.
- `list_messages`/`search` page correctly (`offset`, `total`) and honour `location`, `starred`,
  `has_attachment`; the new query operators are tested including hostile input (the parser never
  raises and never passes user text to FTS5 unquoted).
- The UI contract test (requirement 6) passes against the real output schemas.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
