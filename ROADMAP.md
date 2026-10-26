# Roadmap

What is built is in [CHANGELOG.md](CHANGELOG.md); which numbered build prompts are done or open is
tracked in [CONTRIBUTING.md](CONTRIBUTING.md#status-of-the-prompts). This page is the *why* and the
open questions for what is next. Nothing here is a promise or a date.

## Next

- **Send mail** (prompt 28). One `send_message` operation behind a queued, retried `send` job,
  identities derived from accounts, never automatic, off over MCP until you turn it on. Sync will
  also run any send waiting out a retry delay. *Open:* where a sent copy lives for SMTP-only routes.
- **Pipeline management** (30). Pausing and tuning each stage, and interactive questions taking
  priority over background enrichment, so `oaken ask` does not time out while a backlog is enriching.
  *Open:* how a model call yields without starving background work.
- **Settings you can edit safely** (23, 29, 31, 32). Per-plugin settings, core settings, workspaces
  and MCP exposure edited from the console and the CLI through one comment-preserving config writer.
- **Workspaces by rule.** A Sieve rule decides which workspace a message belongs to, so sensitive
  mail only ever meets a more secure (local-only) model, embedder and search scope. *Open:* how
  retrieval is kept from returning a more sensitive message into a less secure model's context.
- **Agent enrichment** (33): let an external model enrich a message through the agent door and store
  the answer.
- **Security review** (19): the dedicated pass, including the opt-in remote bind.

## Later

- **Label and folder write-back.** Core's labels and folders are its own. An optional, per-provider
  setting would write them back (Gmail labels), through the same durable intent, retry, audit and
  undo path read state uses. *Open:* how nested folders map onto flat Gmail labels.
- **A guided Gmail sign-in** for people who do not live in a terminal: a sign-in dialog in the web
  console and a doctor that explains what to fix (client type, API not enabled, permission not
  granted). *Open:* whether to ever ship an Oaken-owned OAuth client. That makes Oaken a broker of
  every user's Gmail grant and brings Google's verification burden, so the default stays
  bring-your-own.
- **More providers**, such as an `.mbox` import, each offering its own commands. *Open:* how a
  source that is not a live account chooses its datastore and workspace.
- **Pluggable storage.** The raw store and index are core classes today.
- **Starting plugins concurrently**, so a cold local model does not delay core's startup.

## Not planned yet

A legacy-database importer, packaging (systemd, launchd, installers), encryption at rest, plugin
sandboxing, and embedding-throughput tuning on CPU.
