# 37 — Gmail sign-in: the standard consent flow, least-privilege permissions, and a doctor

**Read first:** `docs/prompts/00-context.md`, [11 — gmail plugin](PLUGIN_11-gmail-plugin.md),
[35 — onboarding](CORE_35-onboarding-0-1-1.md) (the Gmail section, which holds the ceiling you must not
re-litigate) and [36 — after 0.1.1](CORE_36-after-0-1-1.md).
**Depends on:** 11, 23 (the `capabilities` allow-list), 24 (accounts), 35.

> **Status: written, not built.** This is the plan for a later session. Nothing in `src/oaken_gmail`
> has been changed for it. Written 2026-10-24 after reading the plugin; the findings below are
> evidence from that read, so check them against the code before relying on a line number.

## Goal

A person signs in to Gmail the way they sign in to anything else:

> Oaken opens the browser to **Google's sign-in**, Google shows **"Oaken Mail wants access to:
> `$x`"**, they press **Approve**, and Oaken has what it asked for and nothing more.

No pasted codes, no token ever shown in a terminal. The flow already exists (PKCE with a loopback
redirect; `oaken gmail auth` prints or opens the address and waits). This prompt makes it honest and
supportable: the permissions asked for match what the account will actually do, the failures say
what to do next, and the client file is easier to live with.

## Decisions already taken (Drew, 2026-10-24; do not re-litigate)

- **The standard flow**, as above. Not a paste-a-code flow, and not a web-console modal in this
  prompt.
- **Bring-your-own OAuth client stays the default.** The consent screen shows the *app name given in
  the person's own Google Cloud project*, so the setup steps must say to name it **Oaken Mail**. A
  vendor-owned, verified client (so there is no Cloud Console step at all) is a plugin-level
  exception for a managed deployment, decided separately (see 35); it is not built here.
- **Build:** permissions that follow capabilities; `oaken gmail doctor`; a better consent
  experience; and importing a private copy of the client JSON.
- **Not now:** a guided `oaken gmail setup` wizard (the doctor and good error messages come first).
- **Import a private copy of the client JSON.** `oaken gmail import-client --file PATH` copies it
  (owner-only) to `<config dir>/gmail_client_secret.json`, which becomes the **default** when
  `client_secret_file` is not set. The download can then be deleted. (Drew's own client is already at
  exactly that path.)

## Evidence: what the plugin does today

- **One scope, always.** `SCOPE = ".../auth/gmail.modify"` in `config.py`, used by `oauth.py`
  (`authorization_url`, and `exchange_code` which refuses a grant that lacks it). A read-only or
  sync-only setup still asks Google for permission to change mail.
- **The flow.** `oauth.authorize(creds, store, http, show_url)` starts a loopback server on
  `127.0.0.1:0`, hands `show_url` the Google address (`access_type=offline`, `prompt=consent`), waits
  up to 300 s, answers the browser with a one-line page, exchanges the code, and stores only the
  refresh token (OS keyring, `oaken` / `gmail:<account>`). `GmailProvider.authorize` wraps it;
  `BaseProvider._auth_consent` turns it into `oaken gmail auth` (output convention `open_url`, then
  `poll` and `poll_input`; the generic CLI follows it; `--no-browser` and `--timeout` exist).
- **Tokens.** `tokens.TokenManager._refresh` posts the refresh grant; Google's reply carries a
  `scope` field that is **ignored today**. `invalid_grant` marks the account as needing
  re-authorization (`REAUTH_HELP` names the Testing-mode seven-day expiry); `invalid_client` is a
  settings error.
- **Client file.** `oauth.load_client(path)` reads `installed` or `web` from the JSON and fails with
  one message that says to set `client_secret_file`. With no `[plugins.gmail]` table the plugin shows
  as "not configured". It never copies the file.
- **Per-account commands are offered while the plugin is unhealthy** (`PluginOperation.per_account`),
  which is what lets `auth` work before sign-in. They run on the account's own instance even if its
  `start()` failed, but then `self._creds`, `_http` and `tokens` do **not** exist. A command that must
  work in that state (`import-client`, `doctor`) cannot lean on a started plugin.
- **Capabilities are already narrowed by the user.** `inferred_capabilities(provider, "provider")`
  honours the `capabilities = [...]` allow-list (built in 23), so "what will this account do" is one
  call. `flags.write` is what needs write access; `send` is never routed in v1.
- **Test fakes.** `src/oaken_gmail/tests/conftest.py` has `FakeGoogle` with a `granted_scope`
  field, a scriptable token endpoint and an in-memory keyring (`MemoryStore`), so every case below
  can be tested with no network.

## Requirements

### 1. Permissions follow capabilities

Ask Google for the least that covers what the account will do, from the effective capabilities
(declared, narrowed by the allow-list):

| The account may | Scopes requested | The consent screen says (Google's words vary; ours, for messages and docs) |
|---|---|---|
| only read (`sync.pull`) | `gmail.readonly` | read your email |
| write flags back (`flags.write`), with or without `send` | `gmail.modify` (covers read, labels and send) | read and change your email: labels, read state, archive, trash; never delete permanently |
| `send` but not `flags.write` | `gmail.readonly` and `gmail.send` | read your email; send email as you |

- A scope helper in `config.py` (`scopes_for(capabilities)` and a plain-words `describe(scope)`);
  `SCOPE` stays as the modify constant so existing imports work.
- `authorization_url` and `authorize` take the scopes. After the code exchange, check the granted
  scopes cover the needed ones (**`gmail.modify` covers `readonly` and `send`**). Google's granular
  consent lets a person untick a permission, so a partial grant must be refused with the missing
  permissions named in plain words, and the stored token must not be treated as good.
- `TokenManager._accept` records the `scope` of a refresh reply (`granted_scopes`). `authenticate`
  and `health` compare it with what is needed and, if it is narrower, report: *authorized with fewer
  permissions than enabled (missing: …); run `oaken gmail auth`*. So widening the allow-list later
  tells the person to sign in again; narrowing it does not force anything.

### 2. `oaken gmail doctor`

A per-account, read-only command (`@command("doctor", ..., per_account=True)`), usable when the
plugin is unhealthy and when it never started (see Evidence). It returns a list of checks, each with
`name`, `ok` (`true`, `false`, or `null` for information), `detail` and `fix`, and a `report` string.
Checks, in order, stopping early where later ones cannot run:

1. **OAuth client**: the file (the configured path, else the default location) exists, parses, and is
   a **Desktop** (`installed`) client. A `web` client cannot use a loopback redirect: say to create a
   Desktop client.
2. **Stored sign-in**: a refresh token is in the keyring (never its value).
3. **The token works**: a refresh succeeds. `invalid_grant` gets the Testing-mode explanation;
   `invalid_client` points at the client file; a network error says Google could not be reached.
4. **Permissions**: the granted scopes cover what is enabled (section 1), in plain words.
5. **Gmail API**: `users.me/profile` answers. A 403 `accessNotConfigured` / `SERVICE_DISABLED` says
   to enable the Gmail API and links the page. On success it names the signed-in address (the
   person's own, on their own terminal).

Informational lines, not failures: while the app is unverified Google shows an "unverified app"
screen the first time, which the person clicks through; a **Testing**-mode app expires its refresh
token after seven days (the doctor cannot see the app's publishing state; it explains the symptom).

The generic CLI prints the output as JSON. Add one small convention to
`apps/cli/.../operation_cmd.py` (as for `open_url` and `poll`): an output with a string field
`report` prints that text instead, and `--json` still prints the whole output.

### 3. A better consent experience

- A clear page in the browser: connected (and what for), or not connected with the reason and the
  command to try again. Static text only; nothing from the query string is echoed into the page.
- `--no-browser` already exists; make sure the printed address is complete and copyable, and that a
  remote or headless run says plainly that the redirect goes to *this* machine's loopback.
- A closed tab, a refused consent (`access_denied`: say *you chose not to allow access, nothing was
  changed*), a timeout, and a busy port each get one plain message and a clean exit (exit 4, as for
  other sign-in failures). Never a traceback, never a token.
- Token endpoint errors during the code exchange map to plain words (`invalid_client`,
  `invalid_grant` for a reused or expired code).
- Optional `login_hint` (the account's address, if it is known) so Google opens on the right account.

### 4. Import a private copy of the client JSON

- `@command("import_client", ..., per_account=True)` with a `file` input: validate it with the same
  reader as the doctor, write it to `<config dir>/gmail_client_secret.json` (create the directory
  `0700`, the file `0600`, replace atomically), return the path and the next step. It never prints the
  secret and never leaves a copy in the audit hash (the input is a path).
- `client_secret_file` unset now means "the default location if a file is there". The "not
  configured" message becomes: *import your Google client with `oaken gmail import-client --file
  PATH`, or set `client_secret_file`*. A plugin with a file at the default location and no
  `[plugins.gmail]` table starts and is healthy once signed in.
- All Gmail accounts share the one client (one Google Cloud project per person); each account has its
  own keyring token.

### 5. Documentation

Update `docs/providers/gmail.md` (the flow as the person sees it, the app name **Oaken Mail**, the
permissions table, `import-client`, `doctor`), the plugin's `setup_help` and `config_example`,
`config.example.toml`, `docs/getting-started.md`, and the changelog.

## Acceptance criteria

- A read-only account (allow-list `["auth", "sync.pull"]`) requests only `gmail.readonly`; the default
  requests only `gmail.modify`; `send` without `flags.write` requests `readonly` and `send`.
- A partial grant is refused with the missing permission named; nothing is stored as good.
- A token that carries fewer scopes than enabled makes `health` report it, with the fix; widening the
  allow-list produces that report, narrowing it does not.
- `doctor` gives one correct answer for each of: no client file; a `web` client; no stored token; a
  dead token (`invalid_grant`); `invalid_client`; missing permissions; Gmail API not enabled; and all
  well. It works on an instance whose `start()` failed.
- `import-client` rejects a file that is not a client JSON, writes `0600` in a `0700` directory,
  replaces atomically, and leaves the source alone; the plugin then starts with no
  `[plugins.gmail]` table.
- Refusal, timeout, a busy port and a closed tab each end with one plain message and exit 4; the
  consent page echoes nothing from the request.
- No token, secret or authorization code appears in any output, log or audit row (a test greps for
  the fake's values).
- `report` prints through the generic CLI and `--json` still gives the whole output.
- `ruff`, `ruff format --check`, `mypy`, `lint-imports`, `pytest` and the docs check are green.

## Open questions (defaults in bold)

- **Q1 — Widening permissions later:** the health report tells the person to run `oaken gmail auth`;
  **nothing re-prompts by itself**.
- **Q2 — The doctor's network calls:** **only to Google's own endpoints**, never logging a token, and
  it makes no change to anything.
- **Q3 — `--client-file` on `gmail auth` too:** **no, one command (`import-client`)** so there is one
  way to do it.
- **Q4 — Several Gmail accounts:** **one shared client, one token each**, as above.
- **Q5 — The guided `gmail setup` wizard:** **later**, once the doctor shows which steps people trip
  on.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
