# 8. Folders are a `location` attribute; labels are tags; a general `enrichment` kind

- Status: accepted
- Date: 2026-10-21

## Context

`message_attrs` in `raw.db` is the generic organization store: rows of `(message_id, kind, value,
source, rule)`, with `kind` constrained by a SQL `CHECK` to
`disposition | label | flag | intent | analysis` (migration 0005). A message's **disposition** is
one of three values (`inbox | filed | discarded`), single-valued per source by a partial unique
index, and the index rejects any other disposition value.

Two needs have emerged:

1. **A mailbox needs folders that are organization, separate from tags that are search.** The UI
   wants a sidebar of folders (where a message lives) distinct from labels (how it is found). Today
   `fileinto "X"` produced a `label`, conflating the two: a filed message's folder was just one of
   its labels, indistinguishable from a search tag.
2. **Future enrichers and plugins will want to attach their own data** (a triage score, a
   plugin-specific marker) without a schema migration each time.

The disposition enum is deliberately small and the index depends on it, so we do **not** widen
disposition or make it free-form. Instead we add a dedicated organization field beside it.

## Decision

Add two `kind` values to `message_attrs` (migration 0012, a forward-only rebuild of the table's
`CHECK`; SQLite cannot alter a `CHECK` in place):

- **`location`** — the folder a message is organized into: **single-valued per
  `(message_id, source)`**, enforced by a partial unique index mirroring the one for `disposition`.
  A `location` rides with `disposition = filed`: a filed message is *in a folder*. `inbox` and
  `discarded` carry no location. The set of distinct `location` values is the folder list a client
  renders; it is queried from the backend, never assembled in the client.
- **`enrichment`** — a general, **multi-valued** escape hatch for enrichers and plugins. Its
  `value` is namespaced as `"<producer>:<key>=<data>"` so producers do not collide and the rows stay
  greppable and index-friendly. New producers need no further migration. (Format may be revisited;
  recorded as tweakable.)

Disposition, label, flag, intent and analysis are unchanged.

### Sieve mapping changes

- **`fileinto "X"`** → `disposition = filed` **and** `location = X` (organization). It no longer
  emits a `label`. `fileinto "INBOX"` is still a keep (disposition `inbox`, no location).
  `fileinto :copy "X"` → `location = X` with disposition `inbox`.
- **`addflag`/`setflag`/`removeflag`** → the system flags `\Seen` and `\Flagged` remain `flag`
  rows (they mean read / starred and are enforced by read-state, not search). A **keyword** flag
  (any non-`\` value, e.g. `addflag "Work"`) becomes a **`label`** — the search-facing tag. This
  keeps read/starred semantics intact while giving scripts a way to tag for search.
- `discard`, `keep`, `skip_enrich`, the suppressed intents and `stop` are unchanged.

### Folders vs tags, stated plainly

- **Folder** = `location` (organization; one per message; the sidebar). Distinguished from a tag by
  **kind**, not by a naming convention.
- **Tag/label** = `label` (search and filtering; many per message).

## Consequences

- One forward-only migration (0012) rebuilds `message_attrs` with the wider `CHECK` and adds the
  one-`location`-per-source unique index; existing rows are copied unchanged. On a populated
  database nothing is lost; on an empty one it is a no-op beyond the schema. Editing 0005 in place
  is **not** done — the migration runner refuses a changed checksum on an applied database.
- The index stores `location` and the query layer gains a `location` filter beside `disposition`
  and `label`, so `list_messages`/`search` fetch a folder's mail in the backend.
- A client builds its sidebar from a counts/listing operation over distinct `location` values and
  per-label counts — no mail is loaded into the client to derive structure.
- Existing `fileinto` scripts change meaning from "label X" to "folder X". Because no production
  sync has populated a real mailbox yet, the impact is minimal; `reprocess('filter')` re-derives
  attributes from the kept raw bytes with no re-download.
- Plugins still do not touch the databases directly. A later, audited core contract may let an
  enricher emit `enrichment` (and perhaps `location`) rows through the plugin host; that is a
  separate decision, not granted here.
- `kind='location'` and `kind='enrichment'` are added to the core `KINDS` set and the architecture
  and software-map docs move with this change.
