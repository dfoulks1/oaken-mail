# Changelog

## 1.0.1 (alpha)

The WebUI comes in-tree. It was a separate repo (`oaken-webui`); it is no longer separable from Oaken Mail, so it ships here.

- A browser UI (looks like a mail client) on loopback, port 62584, reverse-proxies `/v1/*` to the running daemon. It holds the API token; the browser never sees it. `oaken-mail start` runs core and the UI; there is no separate UI command.
- The command is `oaken-mail` (the package name), not `oknpost`. Core's own console is off unless `config.toml` has `[console] enabled = true`.
- The UI reaches core only through the general API the CLI uses (now `oaken_core.cli`), never core internals or the MCP surface. Every mail action stays a registry operation behind the confirm gate.
- The UI now covers accounts (Gmail, IMAP and SMTP: host, login and security, the password stored in the OS keyring, checked on its own), send, a light theme,
  an Enrich pane, thread summaries and sentiment, bulk mark read, and the plugin settings modal. `oaken-mail start`, `stop` and `restart` cover the UI too (`--fg` keeps core alone).
- Needs oaken-core 1.0.1 and oaken-plugins 1.0.
- Not rebuilt from the original UI: the mail-derived CRM (contacts/people).

## 1.0.0 (alpha)

Oaken Mail rebuilt as a thin layer on oaken-core 1.0 and oaken-plugins 1.0, in place of the original application (which is in git history at `cf3b187`).

- `oaken-mail` is the only command. Mail comes from plugins: Gmail, IMAP and SMTP (one instance per account), the `rfc822` formatter and the `mail` ingestor.
- Only mail goes through: providers must be core's own or tagged `mail`.
- A starting config on first run; the console says "Oaken Mail", with its logo, and calls the default queue `intake`.
- Not yet rebuilt: reading state, folders, Sieve, ask and retrieval, the enrichers and scanners; IMAP is pull only.
