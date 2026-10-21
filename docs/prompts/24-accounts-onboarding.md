# 24 — Accounts and onboarding

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 11, 11b, 11c, 12b, 13, 17, 21, 22, 23.

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
   Plugin-level settings reuse prompt 23's form; account-level settings are their own ops.
7. **MCP ceiling.** Account mutations are `mcp="never"`; reads may be `mcp="off"`.

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
