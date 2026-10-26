# 28 — Compose and send

**Read first:** `docs/prompts/00-context.md`.
**Depends on:** 11, 11b, 12b, 13, 22, 24.

## Goal

Make **Send** work from the UI and the CLI, through whichever provider holds the `send` capability
for the chosen identity (SMTP, or Gmail). Today both providers implement `send(raw_bytes)`, but the
host never routes to it, there is no send operation, no outbox, no identities list and no Sent
handling. This prompt adds all of it, and wires the three compose surfaces (float, modal, page) to
one controller.

Per the user: **send is one operation in the registry, implemented once, and every surface just
calls it**: the CLI, any client over `/v1/operations/send_message`
and MCP. There is no second send path for any client. This **reverses the old rule** that `send`
can never be offered over MCP (the registry rejected it at import; ADR 0004 said "sending is never
exposed"). The reversal is deliberate and lands here, with an ADR that amends 0004: the ceiling
changes from `never` to `off`, never `on` (see "MCP exposure of send").

## Decisions already taken (do not re-litigate)

- Permission class is `send`, **`mcp="off"`**: allowed over MCP but **off until the user turns it
  on**: `[mcp] allow_send = true` and then the operation switched on in Settings → MCP (32), or
  `oaken operations enable send_message`. It is never `on` by default and
  the registry still refuses `send` with `mcp="on"`. It is confirm-gated like any mutation; the UI
  and CLI are a person, so `confirm:true` is honoured there (the UI shows a recipient summary first,
  client modal, not the gate), while an MCP caller always gets the two-step proposal.
- Secrets only in the keyring; SMTP password never in config or logs. TLS required, certificates
  verified, no plaintext (11b).
- Bcc goes in the envelope only, never in transmitted headers (11b).
- A receive-only account (prompt 24) is never offered as a send route.
- Compose is **plain text** this phase. No server drafts (`gmail.modify` cannot write them). No
  send-later, no PGP.
- Send is never automatic: no job, schedule, enricher or plugin sends mail on its own. The only
  trigger is a `send_message` call from a caller the user has permitted (the user in the UI/CLI, or
  an MCP client the user has switched send on for).

## Added after 0.1.1 (the job queue and sync)

- **The outbox is the job queue.** A queued send is a `send` job, so it shows in `oaken jobs`
  beside sync and ingest jobs, with leases, retries with backoff and an audit trail.
- **`oaken sync` also runs waiting sends.** A send job runs as soon as a worker picks it up; what a
  sync adds is "run now" for the account's sends that are waiting out a retry delay (the queue's
  existing retry-now path). Failed-for-good sends stay parked, where `retry_parked` handles them.
  Bare `oaken sync` already queues a pull for every account on every provider (`sync_now` with no
  account); with sends it would also run the waiting sends of every account that can send.
- **Receive-only accounts never send**, even from a queue: the `may_send` interlock sits in front of
  the job. Today `sync_now` with no account skips a send-only account as "does not receive mail";
  that becomes "nothing to send or receive".

## Decisions to make in this prompt (record an ADR)

1. **Identity model.** An **identity** is `(display name, from address, route)`. A route is the
   account whose provider performs the send. Derive identities from accounts that declare `send` and
   are not receive-only; an account may also name another account as its `send_via` (a Gmail account
   receiving while an SMTP account sends). Recommended: identities are **derived from accounts**
   plus an optional `send_via` and `signature` in the account's non-secret config; no new table.
2. **Where the outbox lives.** Recommended: a durable `send` job on the existing job queue (retry
   with backoff, transient vs permanent per 11b), the raw RFC 822 bytes stored in `raw.db`'s blob
   store until accepted, then released. Core records the provider's returned id and an audit row
   (ids and recipient *counts*, never addresses or text).
3. **Sent copy.** After acceptance, either (a) do nothing and let sync pull a Sent folder where the
   provider keeps one (Gmail does; SMTP does not), or (b) store the sent message locally as a
   `location = Sent` attr. Recommended: **(a) for Gmail, (b) for SMTP-only routes**, and say so in
   the UI. Sent is **not** in the sidebar this phase; a stored copy is searchable.
4. **Reply and forward.** The operation takes `in_reply_to` (a `message_id` in Oaken's store); core
   builds `In-Reply-To`/`References`, the `Re:`/`Fwd:` subject and the quoted text from stored data,
   so the client never assembles headers. Quote style (bottom/top-post, wrap at 72) is applied by
   core from settings.

## Requirements (operations first, per prompt 22's rule)

1. **`list_identities`** — `read`, `mcp="off"` (an agent needs it to choose a sender). Returns `{identities:[{identity_id, account_id,
   display_name, address, route_account_id, can_send, reason}]}`. `can_send=false` carries a
   plain reason (receive-only, no credential, provider unhealthy). Never credentials.
2. **`send_message`** — permission `send`, `mcp="off"`, confirm-gated, audited. Input:
   `identity_id`, `to[]`, `cc[]`, `bcc[]`, `subject`, `body` (plain text), optional
   `in_reply_to` (message_id) with `mode: reply|reply_all|forward`, `attachments[]`
   (`{filename, content_type, data_base64}`, with a configurable total cap and a per-file cap),
   `idempotency_key` (the client generates one per compose so a double click or a retry cannot send
   twice). Validates addresses and rejects header injection (CR/LF in any header field), an empty
   recipient set, and a send from an identity that cannot send. Returns `{job_id, created}`.
   A preview (`Preview`) lists recipient **count** and attachment names/sizes, not addresses.
3. **`send_status`** — `read`: `{state: queued|sending|sent|failed, per_recipient[], provider_id,
   error_kind: transient|permanent|None}` for a `job_id`. The UI polls it or follows SSE
   (`send.*` events; ids and states only).
4. **Host routing.** The plugin host routes `send` to the provider that declares `send` for the
   route account, and only for a `send_message` job. A test proves a provider is never asked to send
   from any other path, and that a receive-only account is never a route even if its provider
   declares `send`.
5. **Events** `SendQueued`, `SendFinished`, `SendFailed` in `oaken.core.events`: ids, state and
   counts only.
6. **Reply data.** `get_message` already gets `to_addrs`, `cc_addrs`, `message_id_header`,
   `references` in prompt 22; if not, add them here.
7. **CLI.** `oaken send --from IDENTITY --to … --subject … [--body -|--file F] [--reply-to ID]
   [--attach F]…`, `oaken identities`, `oaken send-status JOB`. Add rows to 17's table. The CLI
   needs a line of confirmation of the recipient count before it sends unless `--yes`.
8. **MCP exposure of send.** `send_message`, `send_status` and `list_identities` are ordinary
   registry operations: they appear in the MCP tool set only when the user has enabled them through
   the existing exposure store (`set_operation_exposure`, which stays `mcp="never"`). Change the
   registry check from "send must be `never`" to "send must not be `on`" and keep `/mcp/agent` (the
   job-lease door) without them. Over MCP the call is two-step: the first call returns a proposal
   (recipient count, attachment names and sizes, subject), the second carries the
   `confirmation_token`. **Be honest in the docs and UI about what that gate is:** the token is held
   by the same agent, so it proves the agent re-sent the same input, not that a person saw it.
   The controls are `[mcp] allow_send` (default `false`, edited in Settings → MCP, 32) plus the
   operation's own exposure switch, and the MCP's workspace link (`[mcp] workspaces`). There is **no
   extra MCP-only limit**: an MCP send is the same `send_message` call, same
   job and queue priority, same audit as a UI or CLI send. Record this in the ADR.


## Acceptance criteria

- Against the fake SMTP server and a fake Gmail: a message is sent once, retried on a transient
  failure, parked as failed on a permanent one; a repeated call with the same `idempotency_key`
  does not send twice; Bcc is absent from transmitted headers; a CR/LF in the subject is refused.
- A receive-only Gmail account is never a route; an identity with no credential reports
  `can_send=false` and a reason.
- A reply carries correct `In-Reply-To`/`References` and a quoted body built by core; forward
  carries the original.
- The audit row for a send holds ids and counts only; a test greps it for the address and body.
- The compose UI sends from the float, the modal and the page; a failed send keeps the text; no
  identity → the explanatory message. The three views are driven by one controller (a JS test).
- With exposure at its default, `send_message`, `send_status` and `list_identities` are absent from
  every MCP surface; after the user enables them they appear on `/mcp` (never on `/mcp/agent`); an
  MCP caller cannot send without the two-step confirmation; the registry still refuses `send` with
  `mcp="on"`; and the CLI and MCP both reach the same handler (a test calls it three ways and
  checks one send, one audit row each).
- The parity test from 22 covers `list_identities`, `send_message`, `send_status` for CLI and UI.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green; ADR added.

## Open questions (defaults in bold)

- **Q1 — Attachment caps:** **10 MB per file, 20 MB total**, configurable; base64 over the JSON API.
- **Q2 — Sent copy for SMTP-only routes:** **store locally as `location = Sent`**, not shown in the
  sidebar this phase.
- **Q3 — `send_via`:** **optional per account**, as above, rather than a global default identity.
- **Q4 — MCP policy for send: decided.** `[mcp] allow_send` in `config.toml`, edited in 32's MCP
  textarea; no MCP-only rate limit or allowlist; MCP send uses the same API path and priority as
  UI/CLI send.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
