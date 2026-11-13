# Changelog

## Unreleased

- The browser UI loads nothing from another origin. Bootstrap 5.3.3, Bootstrap Icons 1.11.3 and the Outfit and Figtree fonts are vendored under
  `assets/vendor/` (licenses in `NOTICE`), and the page's Content-Security-Policy allows `'self'` only. Before, every page load fetched them from
  jsDelivr and Google Fonts with no integrity check, in a page that can read all mail through the proxy.

## 1.0.1 (alpha)

The WebUI comes in-tree. It was a separate repo (`oaken-webui`); it is no longer separable from Oaken Mail, so it ships here.

- `oknpost-webui` serves a browser UI (looks like a mail client) on loopback, port 62584, and reverse-proxies `/v1/*` to a running `oknpost` daemon. It holds the API token; the browser never sees it. Two processes: `oknpost start`, then `oknpost-webui`.
- The UI reaches core only through the general API the CLI uses (now `oaken_core.cli`), never core internals or the MCP surface. Every mail action stays a registry operation behind the confirm gate.
- Not rebuilt from the original UI: send, the mail-derived CRM (contacts/people).

## 1.0.0 (alpha)

Oaken Mail rebuilt as a thin layer on oaken-core 1.0 and oaken-plugins 1.0, in place of the original application (which is in git history at `cf3b187`).

- `oknpost` is the only command. Mail comes from plugins: Gmail, IMAP and SMTP (one instance per account), the `rfc822` formatter and the `mail` ingestor.
- Only mail goes through: providers must be core's own or tagged `mail`.
- A starting config on first run; the console says "Oaken Mail", with its logo, and calls the default queue `intake`.
- Not yet rebuilt: reading state, folders, Sieve, ask and retrieval, the enrichers and scanners; IMAP is pull only.
