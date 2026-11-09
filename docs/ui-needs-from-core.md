# What the UI still needs from core and the plugins

State as of 2026-11-09. The adapter (`src/oaken_webui_core/ui_ops.py`) reshapes core's mail operations to what the page reads.
Where core lacks a field, the adapter works around it and says so with a `shortcut:` comment. These are the real fixes.

## 1. `mail.list` and `mail.search` return too little (oaken-plugins, `ingestor_mail`)

Today a row has `subject_id, thread_id, sent_at, from_addr, subject, is_read, attachments`.

| Need | Why | Today's workaround |
|---|---|---|
| `from_name` | the list and thread cards show a person, not an address | one `mail.get` per visible row (`_enrich`) |
| `snippet` | list preview, collapsed thread cards | same `mail.get` |
| `starred`, `labels`, `folder` | star, folder and label chips in the list | one `label_get` per visible row |
| `offset` on `ListIn` and `SearchIn` | the list stops at 200; `PAGE` in `ui_ops.py` is the ceiling | none |
| `unread` (exact count) on `ListOut` | sidebar badge counts the newest 200 only (`list_mailbox`) | count in the newest page |
| exact `total` on `search` | `total` is the page size; only the newest `SCAN` (5000) messages are searched | `total_exact: false` |

Change: add the columns to the `ListMessages`, `ListMessagesInThread` and `SearchScan` queries in `queries/messages.sql`
(`from_name` parsed from `headers_json` in Python, or stored at ingest with a migration; `snippet` from
`SUBSTR(body_clean, 1, 400)`), add `OFFSET ?`, add a `CountUnread` query, then `uv run sqlc generate`
(a test fails when `dbgen/` is stale). Then delete `_enrich` and `_labels` from the list path.

Labels in bulk need a core op, for example `label_get_many(subject_ids)`. One round trip beats N.

## 2. Sending

- Gmail `send` exists (`provider_gmail`, 0.3.0). It is unverified against the real API. The `gmail.modify` scope may not
  permit `messages/send`. If Google answers 403, widen the scope and re-authorize.
- SMTP does not save a Sent copy. Gmail does. A UI-level "save to Sent" is needed for SMTP accounts (append over IMAP, or a local Sent label).
- `send` is `mcp="never"` on both providers. Whether send is ever offered over MCP is an open decision.
- The Gmail provider takes `thread_id` for reply threading; SMTP does not. The UI does not pass it yet.

## 3. Reading, still open

- HTML mail renders in a sandboxed frame (no scripts, no remote loads, links inert). Not yet tried in a real browser.
  Links inert and images blocked want a deliberate "open link" and "load images" choice.
- Attachments list with name, icon and size. There is no download: core keeps attachment bytes in the formatter's quarantine
  (mode 0000, scanned later), so a download needs a core operation that releases a scanned file.
- `get_raw` is in the contract and unhandled.

## 4. Handled nowhere yet (in the contract, not in `ui_ops.py`)

`create_folder`, `create_label`, `move_to_folder`, `set_labels`, `sync_now`, `reprocess`, `get_enrichment`, `get_raw`, the account
and plugin operations, and the sieve operations. Star, archive and folder are labels the mail ingestor already declares
(`mail.starred`, `mail.archived`, `mail.folder`); there is no UI operation to set them. See the actions discussion.

## 4b. Re-running a stage for one message (core: enrich and act built, 2026-11-09; the rest open)

The UI wants re-triage, re-enrich and re-act on a single message, from the message row, the reading pane and the badge lists.
Core cannot do that today:

- `queue_requeue` puts **failed, cancelled or queued** jobs back. It does not re-run a job that succeeded.
- `retriage=true` sends **intake jobs** back to triage, again only for jobs that are requeueable.
- Enrichment is cached by the subject's content ("a rerun is a cache hit", `oaken_core/stages.py`), so re-running an enricher
  without clearing its cache recomputes nothing.
- Nothing runs one named plugin (one triager, enricher or action) against one stored subject.

Built in `oaken-core` (unrun): the envelope takes a `rerun` key, trusted senders only; the `item_rerun` operation submits it; intake
then skips straight to enrich or act on the stored message. Still open: `plugin` (one enricher) and `fresh` (bypass the cache), re-triage
(the raw message is not kept), and `routes` for act (a stored message does not keep what its triagers asked for). The original need:
an operation such as `subject_rerun(subject_id, stage, plugin=None, fresh=False)`. `fresh` bypasses the enrichment cache.
It would clear that plugin's labels for the subject (the human layer stays), queue the stage job, and respect `confidential`
(a confidential subject never goes to a hosted model). Until it exists the UI offers retry only for a failed job (Jobs tab).

## 4c. Editing an existing plugin's settings (core gap)

`plugin_add` takes a config and `plugin_remove` deletes an instance, but no registry operation changes the settings of one that
exists (the console edits `config.toml` its own way). The UI stage page can list, switch, add and remove, not edit. Needed:
`plugin_configure(plugin, config)` that checks the whole change, shows a preview, writes `config.toml` and reloads.

## 5. Open design questions (undecided; answers to be filled in by the owner)

### 5.1 Whose vocabulary: the page's or core's?
`ui_ops.py` renames core's operations and fields (`subject_id` to `message_id`, `mail.list` to `list_messages`) because the page
was written against an older API. The cost is paid twice: every core change needs an adapter change, and every page change needs
a contract change. Options: move the page to core's names, grow core to the page's names, or keep the adapter as the deliberate seam.
Decision: _open_

### 5.2 Where state lives, and how an action reaches the provider
Read, star, archive and folder are local labels on the message. Today only read is written back: `_mark` sets it locally, then asks
the owning provider (`<instance>.set_read`), which pushes only when its `write_back` is on. If the provider call fails the two
differ until the next change (no retry queue). Questions: is every action "local first, then provider"? Is a failed write-back
retried, shown, or both? Can an action be local-only by choice (an archive that Gmail never hears about)? Are delete and trash
local labels, provider calls, or both?
Decision: _open_

### 5.3 Which provider owns a message
`_mark` finds the instance by parsing `source` ("gmail:<account>" yields the account) and calling `<instance>.set_read`. It breaks
if an instance is named differently from its account, and it names a provider in a comment. Options: core exposes "the provider
behind this message" as a lookup; or `mail.get` returns the owning instance name; or the message carries it as a label.
Decision: _open_

### 5.4 What an action is, in the contract
Today a UI operation is one function in `ui_ops.py` that may make several core calls (`mark_read` makes up to 2N+1). Questions:
should an action be one core operation with a preview and a confirm (as `send` is), so the gate sees the whole change; and
should bulk actions return a proposal over a threshold (the page already handles `confirmation_required`)?
Decision: _open_

### 5.5 Who may extend the envelope, and why
Core's intake envelope (`{"oaken": 1, "body": ...}`, `oaken_core/intake.py`) carries `format`, `key`, and, from trusted senders only,
`labels`, `parent` and `hops`. A re-run request (`rerun: {stage, plugin, fresh}`) would be a new key. Open: which senders may add
keys to it (any non-provider plugin, or a named few); what each key is for and who acts on it; whether `hops` gets a enforced
maximum (nothing reads it today); and whether an extension is a core release or something a plugin may declare.
Decision: _open_

## 5. A pre-storage intake kind (decided 2026-11-09: not built)

Sieve and Filters run as triagers, in the Triage stage, and stay there for now. The wish: a plugin kind of its own for the sorting that
happens before storage ("check your papers, process you in"): deterministic gates (Sieve, Filters, and later spamfilter, rules, dedupe)
that decide what is stored, ahead of any judgement by a triager, permissive by default.

Notes for whoever builds it:

- Reject at this point is irreversible: nothing is stored, core keeps a reason and a hash. Ship a "Rejected" view with it, and let a rule
  discard (store, label, hide) before it rejects.
- Core already orders triager stages by creation and runs a triager with no `stage` in the first one (`daemon/core.py:_triagers`), so a
  second triager queue ahead of `triage` would pull every unpinned triager into it. A new kind avoids that.
- The kind is the contract (what a plugin may answer), the stage is the role. Keep `triager` as it is; add the new kind beside it.
