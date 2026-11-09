# Oaken Mail

A local-first, AI-forward mail client, built as a coat of paint on [Oaken Core](https://github.com/dfoulks1/oaken-core). Core is the
pipeline, the plugin loader, the console and the doors (CLI, API, MCP); mail itself is plugins from
[oaken-plugins](https://github.com/dfoulks1/oaken-plugins). This package adds a name, a starting config and one rule: **only mail goes through**.

**Alpha.** It reads real mail from Gmail today; IMAP and SMTP have only been tested against fakes.

```bash
pip install oaken-mail          # or: uv tool install oaken-mail
oknpost start                   # the daemon, in the background
```

Then open the console at <http://127.0.0.1:62536/> (this machine only). `oknpost` is core's CLI under Oaken Mail's name; `oknpost-webui` serves the browser UI (below).

## Add an account

Gmail, IMAP and SMTP are *templates*: one instance per account. Passwords and tokens go in the OS keyring, never in `config.toml`.

```bash
# Gmail: bring your own Google OAuth client (Desktop app type, published to Production)
oknpost plugin add provider.gmail --name me --yes --config '{"account":"me","client_secret_file":"/path/to/client.json","include_labels":["INBOX"]}'
oknpost me auth                 # prints a URL to open in a browser; finishes when Google redirects back
oknpost me check

# IMAP
oknpost plugin add provider.imap --name work --yes --config '{"host":"imap.example.com","username":"you@example.com"}'
oknpost secret set --kind provider --name work --key password
oknpost work check
```

A new account's sync is scheduled when core starts, so restart once (`oknpost restart`) after signing in. `oknpost plugins --kind provider` lists
what you can add.

## The browser UI

`oknpost-webui` serves a browser UI that looks like a mail client. It is a second process beside the daemon: start core, then the UI.

```bash
oknpost start                   # the daemon
oknpost-webui                   # the UI, on http://127.0.0.1:62584/
```

Loopback only, default port 62584 (`--port` to change). It holds core's API token and adds it on the way through, so the token never
reaches the browser; the browser sees one same-origin server. It does not start core: if core is down the UI says so, same as the CLI.
`oknpost-webui` takes `start` (the default), `stop`, `restart`, `status`, and `--fg` to stay attached to the terminal (it runs in the background otherwise).

## What it does and does not

- **Mail only.** A provider loads only if it is core's own or tagged `mail`; the ingestor refuses anything that is not really mail.
- **Not rebuilt from the original UI yet:** sending, and the mail-derived CRM (contacts/people). Reading, search, threads, accounts and Sieve are wired to core operations.
- **Not ported from the original Oaken Mail yet:** ask and retrieval, the enrichers and scanners. IMAP is pull only.
- Your home (config, data) is chosen by `OAKEN_MAIL_HOME`; without it, core remembers the last home it ran in.

## Develop

Oaken Mail develops against checkouts of its parts next to it (`[tool.uv.sources]`): `oaken-core`, `oaken-plugins` and this repository in one folder.

```bash
uv sync && uv run pytest
```

AI-assisted contributions follow the ASF's [Generative Tooling Guidance](https://www.apache.org/legal/generative-tooling.html): see
[oaken-core's CONTRIBUTING](https://github.com/dfoulks1/oaken-core/blob/main/CONTRIBUTING.md). Apache-2.0.
