# Changelog

## 1.0.1 (alpha)

The WebUI comes in-tree. It was a separate repo (`oaken-webui`); it is no longer separable from Oaken Mail, so it ships here.

- `oknpost-webui` serves a browser UI (looks like a mail client) on loopback, port 62584, and reverse-proxies `/v1/*` to a running `oknpost` daemon. It holds the API token; the browser never sees it. Two processes: `oknpost start`, then `oknpost-webui`.
- The UI reaches core only through the general API the CLI uses (now `oaken_core.cli`), never core internals or the MCP surface. Every mail action stays a registry operation behind the confirm gate.
- The UI now covers accounts (Gmail, IMAP and SMTP: host, login and security, the password stored in the OS keyring, checked on its own), send, a light theme,
  an Enrich pane, thread summaries and sentiment, bulk mark read, and the plugin settings modal. `oknpost start`, `stop` and `restart` cover the UI too (`--fg` keeps core alone).
- Needs oaken-core 1.0.1 and oaken-plugins 1.0.
- Not rebuilt from the original UI: the mail-derived CRM (contacts/people).

## 1.0.0 (alpha)

Oaken Mail rebuilt as a thin layer on oaken-core 1.0 and oaken-plugins 1.0, in place of the original application (which is in git history at `cf3b187`).

- `oknpost` is the only command. Mail comes from plugins: Gmail, IMAP and SMTP (one instance per account), the `rfc822` formatter and the `mail` ingestor.
- Only mail goes through: providers must be core's own or tagged `mail`.
- A starting config on first run; the console says "Oaken Mail", with its logo, and calls the default queue `intake`.
- Not yet rebuilt: reading state, folders, Sieve, ask and retrieval, the enrichers and scanners; IMAP is pull only.
