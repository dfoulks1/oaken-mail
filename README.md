# Oaken Mail

A local-first, AI-forward mail client, built as a coat of paint on [Oaken Core](https://github.com/dfoulks1/oaken-core). Core is the
pipeline, the plugin loader, the console and the doors (CLI, API, MCP); mail itself is plugins from
[oaken-plugins](https://github.com/dfoulks1/oaken-plugins). This package adds a name, a starting config and one rule: **only mail goes through**.

**Alpha.** It reads real mail from Gmail today; IMAP and SMTP have only been tested against fakes.

```bash
pip install oaken-mail          # or: uv tool install oaken-mail   (uvx oaken-mail start runs it without installing)
oaken-mail start                # core and the browser UI, in the background
```

Then open <http://127.0.0.1:62584/> (this machine only). `oaken-mail` is core's CLI under Oaken Mail's name; `start`, `stop` and `restart` cover both
core and the UI. Core's own console is off; put `[console]` / `enabled = true` in `config.toml` to serve it on port 62536.

## Add an account

Gmail, IMAP and SMTP are *templates*: one instance per account. Passwords and tokens go in the OS keyring, never in `config.toml`.

```bash
# Gmail: bring your own Google OAuth client (Desktop app type, published to Production)
oaken-mail plugin add provider.gmail --name me --yes --config '{"account":"me","client_secret_file":"/path/to/client.json","include_labels":["INBOX"]}'
oaken-mail me auth                 # prints a URL to open in a browser; finishes when Google redirects back
oaken-mail me check

# IMAP
oaken-mail plugin add provider.imap --name work --yes --config '{"host":"imap.example.com","username":"you@example.com"}'
oaken-mail secret set --kind provider --name work --key password
oaken-mail work check
```

A new account's sync is scheduled when core starts, so restart once (`oaken-mail restart`) after signing in. `oaken-mail plugins --kind provider` lists
what you can add.

## The browser UI

The browser UI looks like a mail client and is the way in: add accounts, read, send and set plugins there. `oaken-mail start` runs it beside core.

Loopback only, port 62584. It holds core's API token and adds it on the way through, so the token never reaches the browser; the browser sees one
same-origin server. `oaken-mail start --fg` keeps core alone in the terminal, without the UI. If core is down the UI says so.

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
