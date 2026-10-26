# 24 — Accounts and onboarding

> **Superseded (0.1.1): `oaken auth`.** The operations named below for sign-in are history.
> Sign-in is each provider's own command: `oaken gmail auth`, `oaken imap auth`, `oaken smtp auth`
> (`--account-id`, `--check`). A password is never an argument: it is read without echo, or from
> standard input with `--password-stdin`. The core operations `set_account_password`,
> `check_account`, `start_authorization` and `authorization_status` are gone; the provider's
> `<provider>.auth` and `<provider>.auth_status` replace them. See [35](CORE_35-onboarding-0-1-1.md).

**Read first:** `docs/prompts/00-context.md`.
**Depends on:** 11, 11b, 11c, 12b, 13, 17, 22, 23.

## Goal

Add and manage mail accounts from a client (UI and CLI): create an account, authenticate it
(Gmail OAuth, IMAP/SMTP passwords), test it, and configure its per-account options — including a
Gmail account set to **listen but not send** (receive-only). Onboarding is the first thing a new
user does, so the UI flow must be complete and honest about what each step touches.

## Context (what already exists)

Account operations are largely built: `create_account`, `get_account`, `set_account_config`,
`set_account_password`, `check_account`, `list_accounts`, and the OAuth pair
`start_authorization` / `authorization_status`. Secrets live only in the OS keyring; `config_json`
on an account is **non-secret only**. Per the architecture, **send is never exposed over MCP and is
a later phase**, so "listen but not send" is mostly about making the receive-only posture explicit
and preventing a send capability from being enabled for that account.

## Requirements (operations first)

1. **Audit the account operations** against the UI onboarding flow and fill gaps with new
   operations (not client logic). Likely additions:
   - a **capability setter** so an account can be marked **receive-only** (sync + flags, no send),
     stored in the account's non-secret config and honored by the provider host when it decides
     which capabilities to route. For Gmail this is the "listen but not send" switch; a test proves
     a receive-only account is never offered as a send route.
   - `remove_account` / `disable_account` if not already present, audited.
2. **Gmail "listen but not send."** A Gmail account can be configured receive-only. Core must refuse
   to route a send for it even if a send operation later exists; the switch lives in account config
   (non-secret) and is surfaced by `get_account`. Default for a newly-added Gmail account should be
   the user's choice during onboarding, defaulting to receive-only until send is a shipped feature.
3. **Auth flows through core.** The UI/CLI drive `start_authorization` → open the URL →
   `authorization_status` for Gmail; `set_account_password` for IMAP/SMTP. The browser never sees
   refresh tokens or passwords — they go straight to the keyring via core; the client only sees
   state (`needs-auth`, `ok`, `invalid_grant`, …).
4. **Test before commit.** Onboarding uses `check_account` to verify an account works (reachable,
   credentials accepted) before it is relied upon, same spirit as 23's `check`.
5. **CLI** verbs cover the whole flow (`oaken account add|auth|check|set|remove …`), or the generic
   `op` fallback plus named verbs for the common ones; update 17's table.
6. **UI** onboarding: pick a provider (Gmail / IMAP+SMTP / IMAP-only), authenticate, test, set
   per-account options (display name, receive-only, sync defaults), and land in an (empty) mailbox.
   Plugin-level settings reuse prompt 23's TOML textarea section; account-level settings are their own ops.
7. **MCP ceiling.** Account mutations are `mcp="never"`; reads may be `mcp="off"`.
8. **The Settings → Accounts pane, mapped.** Build exactly this:
   - **List:** `list_accounts` for the rows, `get_account` per row for provider settings and
     `problem`, `list_schedules` for "synced N min ago", state and cadence. The status dot is derived
     from `problem` and the schedule's `paused`/`last_result`, in one client function. The
     capability tags (`auth`, `sync.pull`, `flags.write`, `send`, `folders`) come from
     `list_plugins` for that provider, **minus `send` when the account is receive-only**. The
     mockup's "cursor 184213 · 12,904 messages" text is fiction: show only what an operation returns
     (message count would need a count in `get_account` or `status`; add it or omit it).
   - **Add account** dropdown: Google (Gmail API), IMAP mailbox, SMTP (send only). Each opens a short
     wizard: `create_account` → credential (`start_authorization` + poll `authorization_status`, or
     `set_account_password`) → `check_account` → options → done. A wizard step that fails keeps the
     account created-but-unusable and says so; it never claims success. The password input posts
     straight to `set_account_password` and is cleared; it is never stored in JS state beyond the
     request.
   - **Pause / Resume / Sync now:** `pause_schedule` / `resume_schedule` / `sync_now` (exist).
   - **Re-authenticate:** `start_authorization`, shown for an `invalid_grant`/`needs-auth` state.
   - **Manage:** a panel with `get_account.config_schema` rendered by `Oaken.form.fromSchema`, the receive-only switch, display name, `send_via` and `signature` (28 reads these),
     and **Remove**.
   - **SMTP (send only)** has no mailbox: no sync button, no schedule, shown as "Ready" from
     `check_account`.
9. **Send routing interlock (with 28).** The receive-only setting must be the same non-secret key
   28 reads to compute `can_send` for `list_identities`. One key, one reader; add a test in 28's
   suite that flips it and watches the identity change.
10. **Open questions (defaults in bold):**
    - Q1: a newly added Gmail account defaults to **receive-only** until the user turns send on
      (28 ships send, so this default is a visible choice in the wizard, not a hidden one).
    - Q2: `remove_account` **keeps the mail** already stored unless the user also asks to purge it;
      purge is a separate confirmed operation and not built here.

## Acceptance criteria

- A new Gmail account can be created, authorized (fake OAuth in tests), checked, and configured
  **receive-only**; a test proves core will not route a send for it.
- IMAP/SMTP accounts can be added with passwords that go only to the keyring; a test proves no
  secret is ever returned by `get_account` or written to `config_json`.
- The full onboarding flow is drivable from the CLI and the UI against the same operations; the
  parity test from 22 covers them.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
