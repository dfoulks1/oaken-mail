# 27 — Contacts / people

**Read first:** `docs/prompts/00-context.md` and `docs/prompts/21b-webui-client-foundation.md`.
**Depends on:** 06, 07, 13, 16, 17, 21, 21b, 22.

## Goal

Give the UI (and CLI) a **people** view: the correspondents a user deals with, drawn from their
mail — names, addresses, how often and how recently they have corresponded, and a way to see a
person's messages. Today `contacts.html` is a pure mockup and there is **no contacts store or
operation**. This is the largest of the UI prompts and starts with a design decision.

## Decision to make in this prompt (record an ADR)

- **Derive vs. store.** Two options:
  1. **Derive** people from the index on demand — aggregate senders/recipients from parsed
     messages (addresses, display names, counts, last-seen), no new table. Simplest, always
     consistent with the mail, nothing to migrate; costs an aggregate query per view.
  2. **Store** a contacts table (people + addresses, possibly user-edited names/notes, derived
     stats refreshed on ingest). Richer (editable, groups), but a new store, new write paths, and a
     derive-vs-edit reconciliation story.
  Prefer **derive** for v1 unless the user wants editing/notes now; record the choice and why. If
  derived, identity is the email address (folded/normalized), with display names collected and the
  most common/most recent chosen. No new mutable store means no secret/PII write concerns beyond
  what the mail already holds.

## Requirements (operations first)

1. **`list_people` / `get_person`** — `read`. `list_people` returns correspondents with address,
   best display name, message count, unread count, last-seen, and the accounts they appear in,
   paged and sortable (by recency or frequency). `get_person(address)` returns the detail plus a
   handle to list their messages. Both treat mail as untrusted (names/addresses are data) and never
   mark anything read.
2. **Person → messages.** Selecting a person drives `search`/`list_messages` with a
   `from:`/participant filter (the index already matches `from:` by name or address). Add a
   participant filter to the search filters if one is not already sufficient; prefer reusing what
   exists.
3. **Privacy.** People data is mail-derived and local. If the derive route is chosen, there is no
   new place PII is written. If the store route is chosen, the ADR must cover where it lives
   (`raw.db` vs `index.db`), that it is rebuildable or user-owned, and that it never leaves the
   machine.
4. **MCP ceiling.** `list_people`/`get_person` are `read`; a sensible ceiling is `mcp="off"`
   (the user can enable), since a people list is a privacy-relevant aggregate even though it holds
   no message bodies.
5. **CLI** parity: `oaken people [--sort recency|frequency]` and `oaken person ADDRESS`; update
   17's table.
6. **UI**: a contacts page listing people with counts and last-seen, search/sort, and a person
   view that lists their messages (reusing the message list component) — replacing the mockup with
   real data, and showing the empty state when there is no mail yet. Concretely (from 21b A6):
   - **Left list:** `list_people {query, sort, limit, offset}`; the "Filter contacts" box is the
     `query` (debounced, server-side; names and addresses are untrusted text, inserted as text).
     Each row: initials avatar, best name, address, message count.
   - **Right panel:** `get_person {address}` → name, address(es), counts, first/last seen, accounts;
     "Recent messages" is `Oaken.list.mount` (21b C6) over `list_messages {participant}`.
   - **Email button** → `Oaken.compose.open({to:[address]})` (28); it carries
     `data-oaken-requires="send_message"`.
   - **Removed from the live UI** (derived data has nothing to store): the "New" button, the
     Edit button, the contact modal (first/last name, group, notes), the Groups row, the PGP key row
     and the per-person group tag. Their templates may stay for the demo only.
   - **Autocomplete for compose (28):** `list_people {query, limit:8, sort:"frequency"}` is the data
     source for recipient chips; keep the response small and never log the query.
7. **Open questions (defaults in bold):**
   - Q1: counts include **both** sent and received messages for a person (participant), with the
     split available in `get_person`.
   - Q2: addresses that look automated (no-reply, list addresses) are **included but flagged
     `automated:true`** so the UI can hide them by default; no heuristic beyond common local parts
     and a `List-Id` header, recorded in the ADR.

## Acceptance criteria

- On a seeded synthetic mailbox, `list_people` returns the right correspondents with correct
  counts/last-seen, folds addresses consistently, and chooses a sensible display name; `get_person`
  lists that person's messages via the existing search/filter path.
- The chosen derive-vs-store decision is implemented and recorded in an ADR; if derived, a test
  shows results stay consistent with the mail after new ingest with no extra write step.
- People are drivable from the CLI and the UI against the same operations; the parity test from 22
  covers them.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green; ADR added.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
