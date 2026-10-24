+++
title = "Store credentials in your keyring"
subtitle = "Where Oaken keeps passwords and tokens, and how to check that your keyring works"
lead = "Oaken never writes a password or token to a file or an environment variable. It asks your operating system's keyring to keep them, and this page shows what that means, how to check it works, and what to do when it does not."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "start"
order = 11
description = "How Oaken uses the OS keyring: the service and entry names, the commands that store credentials (oaken gmail auth, oaken imap auth), a quick check that the keyring works, how to inspect entries with Seahorse, and what the no-keyring error means."
+++

## What the keyring is

The keyring is an encrypted store run by your desktop session. Programs ask it to save and fetch
small secrets. On Linux it is usually GNOME Keyring or KWallet, speaking the Secret Service
protocol; on macOS it is Keychain; on Windows it is Credential Locker. It is normally unlocked by
your login password when you sign in.

## What Oaken stores there

Each secret is one entry, found by a **service** and a **user** name. Oaken always uses the service
`oaken`; the user is `<kind>:<account>`.

| Provider | Entry user | What is stored |
| --- | --- | --- |
| Gmail | `gmail:<account>` | the OAuth refresh token |
| IMAP | `imap:<account>` | the password |
| SMTP | `smtp:<account>` | the password |

`<account>` is the `account` setting of the provider, which is `default` when you do not set it.
Access tokens are kept in memory only. The Gmail OAuth *client* JSON is not a secret of this kind:
it stays a file, named by `client_secret_file`.

There is no setting that points Oaken at a file or an environment variable for a credential, and
the settings store refuses keys that look like secrets (`token`, `password` and similar).

## Store a credential

You do not write to the keyring yourself. Run the command for the provider:

```
oaken gmail auth --account-id default  # opens Google's consent page, stores the refresh token
oaken imap auth --account-id default   # asks for the password without echoing it
oaken smtp auth --account-id default   # the same
```

For IMAP and SMTP the password can also be piped in with `--password-stdin`. It is never taken as a
command-line argument and never printed. If the account has two-step verification, use an app
password. `--account` may be left out when the provider has exactly one account.

Add `--check` to see whether a credential is stored and works without changing it.

## Check that your keyring works

This stores, reads and removes a throwaway entry that has nothing to do with Oaken:

```
python3 -c "import keyring; keyring.set_password('test','me','hello'); print(keyring.get_password('test','me')); keyring.delete_password('test','me')"
```

If it prints `hello`, the keyring works. If it hangs or fails, it is locked or unreachable.

## Look at the entries

A graphical keyring manager shows what is stored. On GNOME, install **Seahorse** ("Passwords and
Keys") and open the *Login* keyring: entries written by Oaken are labelled with the service
`oaken`. You can delete one there to reset an account, then run the provider's `auth` command again (`oaken gmail auth`). The secret
itself is shown only if you ask and the keyring is unlocked.

## When there is no keyring

Oaken stops with an error such as *no OS keyring is available, so the password cannot be stored
safely*. It does not fall back to a file. Usual causes:

- **The keyring is locked.** Unlock it; a prompt normally appears, and your login password opens it.
- **No Secret Service is running.** Install and start one (for example `gnome-keyring`).
- **No desktop session**, as over SSH or in a container. There is no D-Bus session to talk to. Run
  the command from your desktop session, or start a session with `dbus-run-session` and unlock a
  keyring inside it.
