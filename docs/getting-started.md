+++
title = "Set up Oaken from scratch"
subtitle = "From a fresh checkout to a synced mailbox and a connected AI client"
lead = "This walks through a first setup on one machine: install, the config file, the local model, one mail account, the daemon, a first sync, and, if you want it, the MCP server. Each step says how to check that it worked."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "start"
order = 10
description = "A first-time setup of Oaken Mail: install with uv, write config.toml, pull the Ollama models, add an account, store its credential in the keyring, start the daemon, sync, and turn on the MCP server."
+++

Oaken is pre-alpha and is run from a source checkout. Everything below stays on your machine.

## 1. Install

You need Python 3.11 or newer and uv.

```bash
git clone https://github.com/dfoulks1/oaken-mail
cd oaken-mail
uv sync
uv run oaken --help
```

There is no global install yet, so run commands as `uv run oaken ...` from the checkout, or
activate the virtual environment (`source .venv/bin/activate`) to type `oaken` alone.

## 2. Write `config.toml`

The file lives outside the repository, in your per-user config directory
(`~/.config/oaken/` on Linux). Start from the documented template:

```bash
mkdir -p ~/.config/oaken
cp config.example.toml ~/.config/oaken/config.toml
```

Every section has defaults, so you only keep what you change. Set `OAKEN_HOME=/some/dir` to put
everything under one directory instead. **Never put a password or token in this file**; step 5
stores them in your keyring. See [Store credentials in your keyring](keyring.html) if you have not
used one before.

The template's `[plugins.gmail]` table is live, not commented out. If you do not use Gmail, delete
it, or Oaken will report the Gmail provider as unhealthy for want of a client file.

## 3. Get the local models

Oaken runs a local Ollama by default. It never pulls models itself, so
pull the ones named in `[ai]` (these are the defaults):

```bash
ollama pull nomic-embed-text     # the embedder
ollama pull qwen2.5:3b           # the language model
```

To use a model you already have, set `llm_model` (and `embed_model`) under `[ai]` in `config.toml`.

## 4. Add an account

Pick the provider that matches the account:

- **Gmail**: [Connect Gmail](providers/gmail.html). You create your own Google OAuth client
  (Desktop app, `gmail.modify` scope, published to Production) and save its JSON where
  `client_secret_file` points.
- **Any IMAP server**: [Connect any IMAP server](providers/imap.html).
- **Sending through your own server**: [SMTP](providers/smtp.html).

Then register the account. On a terminal this asks for the provider, an id, a display name and the
provider's non-secret settings:

```bash
uv run oaken admin account add
uv run oaken admin account list
```

The account id (letters, digits, `-` and `_`) names the provider instance, `gmail@<id>`, and its
keyring entry.

## 5. Store the credential

```bash
uv run oaken auth gmail --account <id>     # opens Google's consent page
uv run oaken auth imap --account <id>      # or: asks for the password, no echo
```

The credential goes to the OS keyring and is never printed. Add `--check` to test one that is
already stored.

## 6. Start the daemon and check it

```bash
uv run oaken start --detach
uv run oaken status                  # prints the pid and the loopback port
uv run oaken plugins                 # your account's provider row should say healthy
uv run oaken admin account show <id> # a "problem:" line says why one is not
```

Core serves a small API on loopback only. Its token is the file `api_token` in the config
directory. [The daemon and the local API](architecture/daemon.html) explains the rest.

## 7. Sync and look

```bash
uv run oaken sync --account <id> --wait
uv run oaken search "invoice"
```

The first sync of a large mailbox takes a while by design. Mail is stored under your user data
directory (`~/.local/share/oaken/` on Linux). A schedule then keeps the account current; see
[Sync schedule](architecture/sync-schedule.html).

## 8. Optional: connect an AI client

The MCP server is off until you turn it on. Add this to `config.toml` and restart core:

```toml
[api]
mcp = true
```

Then follow [Connect an AI client](mcp.html) to point Claude Desktop or another client at
`oaken mcp`. Changes made through it always ask for confirmation first.

## 9. Optional: the web UI

With core running, `uv run oaken-webui` serves the web UI on port 47813 of `127.0.0.1`. It is a
separate client that proxies to core and keeps the API token on the server side.

## If something fails

| Message | Likely cause |
| --- | --- |
| `no OS keyring is available` | The keyring is locked or not running. See [the keyring page](keyring.html). |
| `client_secret_file is missing or is not a Google OAuth client JSON` | The file does not exist, or is a `web` client. It must be a Desktop-app client with an `installed` block. |
| `core is already running` | A daemon is up. `oaken status` shows where; `oaken stop` ends it. |
| `core_not_running` from a tool call | Start it with `oaken start --detach`. |
